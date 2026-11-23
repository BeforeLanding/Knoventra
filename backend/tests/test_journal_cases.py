"""Journal case pipeline: redaction, model resolution, dedup, case fields, storage."""
import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from knoventra.db.models import IngestAudit, JournalCase
from knoventra.ingestion.cases import (
    Machine,
    build_cases,
    extract_case_fields,
    hamming,
    load_registry,
    models_from_text,
    near_duplicate,
    redact,
    residual,
    resolve_models,
    simhash64,
)
from knoventra.ingestion.journal import JournalRecord, SourceRef, import_workbooks, parse_workbook

JOURNALS = Path(__file__).resolve().parents[2] / "data" / "demo" / "journals"


def _rec(content, topic="mix tank alarm", notes="", machine="610101", row=2, date=dt.date(2026, 3, 2),
         engineer="Chen Lixing"):
    return JournalRecord(SourceRef("t.xlsx", "s", row), machine, date, engineer, topic, content, notes, "NRS")


@pytest.mark.parametrize("raw, kind, kept", [
    ("进入工程模式（口令：Fake-Eng-0420）更新", "password", "口令：[PASSWORD]）更新"),
    ("用账号 fse_demo 密码 Demo#1234 登录", "password", "密码 [PASSWORD] 登录"),
    ("pwd=abc123; next", "password", "pwd=[PASSWORD]; next"),
    ("联系 FSE 电话 +65 9000 0001", "phone", "电话 [PHONE]"),
    ("客户 IT 电话 13800138000，", "phone", "电话 [PHONE]，"),
    ("台湾手机 0912-345-678 联系", "phone", "[PHONE] 联系"),
    ("邮箱 it-desk@example.test", "email", "邮箱 [EMAIL]"),
])
def test_redaction_rules_hit(raw, kind, kept):
    text, found = redact(raw)
    assert found[kind] == 1 and kept in text
    assert not residual(text)


@pytest.mark.parametrize("safe", [
    "保压 30 分钟，压降 < 0.02 MPa", "DHF 流量设定 1.2 L/min", "机台 610101 于 2026-03-02 装机",
    "alarm A-1301", "SOP-WCL-380110-001 第 4 节", "跑 25 片 dummy wafer", "扭矩 12 N·m",
])
def test_redaction_leaves_ordinary_numbers_alone(safe):
    text, found = redact(safe)
    assert text == safe and not found


def test_models_registry_first_then_dictionary_never_guess():
    registry = {"610101": Machine("610101", "WX-38")}
    assert resolve_models("610101", "双面机 spindle", registry) == (["WX-38"], "registry")
    assert resolve_models("999999", "ST250 door override", registry) == (["ST-250"], "dictionary")
    assert resolve_models("999999", "chamber teaching", registry) == ([], "unknown")
    assert models_from_text("WX 系列 door override") == ["WX-38", "WX-P"]
    assert models_from_text("wx38 單面機") == ["WX-38"]
    assert models_from_text("WX-PM 模块") == []


def test_simhash_is_stable_and_signed_64_bit():
    a = simhash64("1.确认 alarm A-1301 为 LS-2 信号丢失")
    assert a == simhash64("1.确认 alarm A-1301 为 LS-2 信号丢失")
    assert -(1 << 63) <= a < (1 << 63)
    assert hamming(a, a) == 0


def test_near_duplicate_needs_both_simhash_and_jaccard():
    base = "1.确认 alarm A-1301 为 tank level sensor LS-2 信号丢失；2.LS-2 接头松动，重新插紧；3.reset interlock 后 pump 恢复"
    copy = base + "（同上周，继续跟进）"
    other = "1.用水平仪确认 load port 四角水平；2.调整脚座至气泡居中；3.复测 robot 取放片无异响"
    assert near_duplicate(base, simhash64(base), copy, simhash64(copy))
    assert not near_duplicate(base, simhash64(base), other, simhash64(other))


def test_exact_and_near_duplicates_merge_but_models_stay_apart():
    registry = {"610101": Machine("610101", "WX-38"), "610102": Machine("610102", "WX-P")}
    text = "1.确认 alarm A-1301 为 LS-2 信号丢失；2.LS-2 接头松动，重新插紧；3.按 SOP reset interlock 后 pump 恢复运转"
    rows = [
        _rec(text, row=2),
        _rec(text, row=3, date=dt.date(2026, 3, 9)),                       # weekly copy
        _rec(text + "（同上周，继续跟进）", row=4, date=dt.date(2026, 3, 16)),  # near copy
        _rec(text, row=5, machine="610102"),                               # other model
    ]
    drafts, stats = build_cases(rows, registry)
    assert (len(drafts), stats.exact_merged, stats.near_merged) == (2, 1, 1)
    wx38 = next(d for d in drafts if d.models == ["WX-38"])
    assert sorted(o.record.source.row for o in wx38.members) == [2, 3, 4]


def test_case_fields_for_resolved_unresolved_and_procedures():
    resolved = extract_case_fields(
        "mix tank 切换报 alarm A-1301，pump 不转",
        "1.确认 alarm A-1301 为 LS-2 信号丢失；2.检查发现 LS-2 接头松动，重新插紧；3.reset interlock 后 pump 恢复运转", "")
    assert (resolved.category, resolved.resolved, resolved.alarm_codes) == ("troubleshooting", True, ["A-1301"])
    assert "LS-2 信号丢失" in resolved.cause and "reset interlock" in resolved.solution

    pending = extract_case_fields("nozzle 間歇漏液", "1.更換 O-ring 後仍偶發 A-2207；2.懷疑 suck-back valve 延遲；3.待跟蹤", "")
    assert (pending.category, pending.resolved, pending.solution) == ("troubleshooting", False, "")
    assert pending.alarm_codes == ["A-2207"] and "suck-back" in pending.cause

    procedure = extract_case_fields("EMO 测试", "1.逐个按下 EMO；2.复位后检查 alarm 记录；3.填写测试表",
                                    "ST-250 机台注意")
    assert (procedure.category, procedure.resolved, procedure.alarm_codes) == ("procedure", None, [])


def test_demo_data_dedup_never_mixes_scenarios_and_redacts_everything():
    manifest = json.loads((JOURNALS / "manifest.json").read_text(encoding="utf-8"))
    labels = {(r["file"], r["row"]): r for r in manifest["rows"]}
    records = [r for f in manifest["files"] for r in parse_workbook(JOURNALS / f["file"]).journals]
    drafts, stats = build_cases(records, load_registry(JOURNALS / "machines.json"))
    for d in drafts:
        scenarios = {labels[(o.record.source.file, o.record.source.row)]["scenario"] for o in d.members}
        assert len(scenarios) == 1, scenarios
    merged = {(o.record.source.file, o.record.source.row) for d in drafts if len(d.members) > 1 for o in d.members}
    for key, row in labels.items():
        if {"exact_duplicate", "near_duplicate"} & set(row["labels"]):
            assert key in merged
    sensitive = sum(1 for r in manifest["rows"] if "sensitive" in r["labels"])
    assert stats.redactions_found["password"] == sensitive and not stats.redactions_residual
    assert stats.unknown_model == 0


def test_import_is_idempotent_and_audited(db):
    files = sorted(JOURNALS.glob("*.xlsx"))
    first = import_workbooks(db, files, JOURNALS / "machines.json")
    cases = db.scalar(select(func.count()).select_from(JournalCase))
    assert first["counts"]["cases_created"] == cases > 0 and not first["rejected"]
    second = import_workbooks(db, files, JOURNALS / "machines.json")
    assert second["counts"]["cases_created"] == 0
    assert db.scalar(select(func.count()).select_from(JournalCase)) == cases
    total_sources = sum(len(c.sources) for c in db.scalars(select(JournalCase)))
    assert total_sources == first["counts"]["journal_rows"]

    audits = db.scalars(select(IngestAudit).order_by(IngestAudit.created_at)).all()
    assert len(audits) == 2 and audits[0].redactions["residual"] == {}
    assert audits[0].redactions["found"]["password"] > 0
    stored = " ".join(c.content + c.notes for c in db.scalars(select(JournalCase)))
    assert "Fake-Eng-0420" not in stored and "13800138000" not in stored and "[PASSWORD]" in stored


def test_new_near_duplicate_merges_into_stored_case(db):
    from knoventra.ingestion.cases import store_cases

    registry = {"610101": Machine("610101", "WX-38", "CN")}
    text = "1.确认 alarm A-1301 为 LS-2 信号丢失；2.LS-2 接头松动，重新插紧；3.按 SOP reset interlock 后 pump 恢复运转"
    drafts, _ = build_cases([_rec(text)], registry)
    assert store_cases(db, drafts, registry) == (1, 0)
    later, _ = build_cases([_rec(text + "（同上周）", row=9, date=dt.date(2026, 4, 1), engineer="Zhou Lan")], registry)
    assert store_cases(db, later, registry) == (0, 1)
    (case,) = db.scalars(select(JournalCase)).all()
    assert case.dates == ["2026-03-02", "2026-04-01"] and case.engineers == ["Chen Lixing", "Zhou Lan"]
    assert case.occurrences == 2 and case.regions == ["CN"] and case.resolved is True


def test_demo_seed_includes_sops_for_ops_only(db, users):
    from knoventra.access.scope import compute_scope
    from knoventra.db.models import Document, KnowledgeBase, Revision

    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == "field-sop"))
    assert db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.id)) == 10
    mixtank = db.scalar(select(Document).where(Document.kb_id == kb.id, Document.title.like("SOP-WCL-380110-001%")))
    statuses = db.execute(select(Revision.version, Revision.status).where(Revision.document_id == mixtank.id)
                          .order_by(Revision.version)).all()
    assert [tuple(s) for s in statuses] == [(1, "DRAFT"), (2, "PUBLISHED")]
    assert kb.id in compute_scope(db, users["sg-ops"]).readable_kb_ids
    assert kb.id not in compute_scope(db, users["hr"]).readable_kb_ids
