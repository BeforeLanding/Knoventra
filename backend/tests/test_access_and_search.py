import pytest
from sqlalchemy import delete, select

from knoventra.access.scope import NoAccess, compute_scope
from knoventra.db.models import Chunk, Document, KbGrant, KnowledgeBase, Revision
from knoventra.ingestion.service import publish
from knoventra.retrieval.search import search

QUESTIONS = [
    "E17 设备报代码 42 先查什么",
    "年假怎么申请",
    "差旅报销时限",
    "校准证书过期 C21-05",
    "預算上限",
    "VPN 怎么用",
    "感測器訊號漂移 S5-19",
]


def kb_names(db, ev):
    out = set()
    for e in ev:
        out.add(db.scalar(select(KnowledgeBase.name).join(Document, Document.kb_id == KnowledgeBase.id)
                          .where(Document.id == e.document_id)))
    return out


EXPECTED_KBS = {
    "sg-ops": {"ops-global", "it-public"},
    "tw-ops": {"ops-global", "it-public"},
    "cn-ops": {"ops-global", "ops-cn", "it-public"},
    "hr": {"hr", "it-public"},
    "fin": {"fin", "it-public"},
    "admin": {"it-public"},
}


@pytest.mark.parametrize("who", list(EXPECTED_KBS))
def test_only_readable_kbs_ever_returned(db, enc, users, who):
    scope = compute_scope(db, users[who])
    seen = set()
    for q in QUESTIONS:
        seen |= kb_names(db, search(db, enc, scope, q, top_k=20).evidence)
    assert seen <= EXPECTED_KBS[who]


def test_same_question_three_accounts(db, enc, users):
    q = "校准证书过期 C21-05"
    hits = {}
    for who in ("cn-ops", "sg-ops", "hr"):
        ev = search(db, enc, compute_scope(db, users[who]), q, top_k=3).evidence
        hits[who] = {e.document_title for e in ev}
    assert any("校准" in t for t in hits["cn-ops"])
    assert not any("校准" in t for t in hits["sg-ops"])
    assert not any("校准" in t for t in hits["hr"])


def test_scope_fail_closed(db, users, monkeypatch):
    db.execute(delete(KbGrant))
    with pytest.raises(NoAccess):
        compute_scope(db, users["sg-ops"])
    users["hr"].enabled = False
    with pytest.raises(NoAccess):
        compute_scope(db, users["hr"])
    broken = type("S", (), {"scalars": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))})()
    with pytest.raises(NoAccess):
        compute_scope(broken, users["sg-ops"])


def test_exact_fault_code_found_via_sparse(db, enc, users):
    scope = compute_scope(db, users["sg-ops"])
    res = search(db, enc, scope, "E17-42")
    assert res.evidence[0].document_title.startswith("E17")
    assert "sparse" in res.evidence[0].channels
    dense_only = search(db, enc, scope, "E17-42", use_sparse=False)
    assert "sparse" not in dense_only.stages


def test_traditional_query_hits_simplified_manual(db, enc, users):
    scope = compute_scope(db, users["tw-ops"])
    ev = search(db, enc, scope, "泵組壓力不足 P03-11 怎麼處理").evidence
    assert ev[0].document_title == "泵组维护手册"
    assert "泵组" in ev[0].text  # original glyphs preserved


def _e17(db):
    doc = db.scalar(select(Document).where(Document.title.like("E17%")))
    revs = {r.version: r for r in db.scalars(select(Revision).where(Revision.document_id == doc.id))}
    return revs


def test_only_published_revision_is_retrieved(db, enc, users):
    scope = compute_scope(db, users["sg-ops"])
    revs = _e17(db)
    before = search(db, enc, scope, "E17-42 排查步骤", top_k=10).evidence
    assert {e.revision for e in before if e.document_title.startswith("E17")} == {1}
    publish(db, revs[2].id)
    after = search(db, enc, scope, "E17-42 排查步骤", top_k=10).evidence
    assert {e.revision for e in after if e.document_title.startswith("E17")} == {2}
    assert revs[1].status == "RETIRED"


def test_failed_publish_keeps_old_version(db, enc, users):
    revs = _e17(db)
    with pytest.raises(RuntimeError):
        with db.begin_nested():
            publish(db, revs[2].id)
            raise RuntimeError("crash after status flip")
    db.expire_all()
    assert db.get(Revision, revs[1].id).status == "PUBLISHED"
    assert db.get(Revision, revs[2].id).status == "DRAFT"
    scope = compute_scope(db, users["sg-ops"])
    ev = search(db, enc, scope, "E17-42", top_k=10).evidence
    assert {e.revision for e in ev if e.document_title.startswith("E17")} == {1}


def test_retired_revision_cannot_be_republished(db):
    revs = _e17(db)
    publish(db, revs[2].id)
    with pytest.raises(ValueError):
        publish(db, revs[1].id)


def test_draft_chunks_exist_but_not_searchable(db):
    revs = _e17(db)
    n = db.scalar(select(Chunk.id).where(Chunk.revision_id == revs[2].id).limit(1))
    assert n is not None


@pytest.mark.parametrize("code", ["E17-44", "P03-12", "SEAL-9W", "SNS-5C", "E17-420", "XE17-42"])
def test_near_miss_never_returns_similar_code_as_evidence(db, enc, users, code):
    from knoventra.retrieval.pipeline import INSUFFICIENT, retrieve

    out = retrieve(db, enc, None, users["sg-ops"], f"{code} 如何处理", threshold=0.0)
    assert out.status == INSUFFICIENT and out.evidence == []
    assert out.reason == "IDENTIFIER_MISMATCH" and out.missing_identifiers == [code]


@pytest.mark.parametrize("code", ["E17-07", "ｅ１７－０７", "e17‑07", "S5–19", "SEAL−9X"])
def test_exact_identifier_in_text_or_heading_survives_normalization(db, enc, users, code):
    from knoventra.retrieval.identifiers import evidence_identifiers, extract_identifiers
    from knoventra.retrieval.pipeline import OK, retrieve

    out = retrieve(db, enc, None, users["sg-ops"], code, threshold=0.0)
    assert out.status == OK and out.evidence
    assert all(extract_identifiers(code) & evidence_identifiers(e.section_path, e.text) for e in out.evidence)


def test_exact_code_in_unreadable_kb_is_not_considered_present(db, enc, users):
    from knoventra.retrieval.pipeline import INSUFFICIENT, OK, retrieve

    denied = retrieve(db, enc, None, users["sg-ops"], "C21-05", threshold=0.0)
    allowed = retrieve(db, enc, None, users["cn-ops"], "C21-05", threshold=0.0)
    assert denied.status == INSUFFICIENT and denied.missing_identifiers == ["C21-05"]
    assert allowed.status == OK and allowed.evidence
