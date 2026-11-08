from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import runpy
import threading
import time
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from knoventra.db.models import Chunk, Document, KnowledgeBase, Revision
from knoventra.ingestion.service import add_revision, publish


@pytest.fixture
def revision_case(engine, enc, tmp_path):
    """Committed data for genuinely independent publisher/upload transactions."""
    path = tmp_path / "manual.md"
    path.write_text("# 并发版本测试\n## 处置\n检查设备日志。", encoding="utf-8")
    with Session(engine) as db, db.begin():
        kb = KnowledgeBase(name="revision-test-" + uuid.uuid4().hex)
        db.add(kb)
        db.flush()
        kb_id = kb.id
        revisions = [add_revision(db, enc, kb_id=kb_id, title="并发手册", lang="zh-Hans", path=path)
                     for _ in range(3)]
        publish(db, revisions[0].id)
        ids = [r.id for r in revisions]
    try:
        yield kb_id, ids, path
    finally:
        with Session(engine) as db, db.begin():
            docs = select(Document.id).where(Document.kb_id == kb_id)
            db.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
            db.execute(delete(Revision).where(Revision.document_id.in_(docs)))
            db.execute(delete(Document).where(Document.kb_id == kb_id))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))


def wait_for_blocker(engine, blocked_pid, blocker_pid):
    # Observe a real PG lock wait rather than rely on a timing-only sleep.
    deadline = time.monotonic() + 5
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            if blocker_pid in conn.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked_pid}):
                return
            time.sleep(0.01)
    pytest.fail("concurrent publisher did not wait for the document lock")


@pytest.mark.parametrize("target_index, expected", [(2, "PUBLISHED"), (0, "REJECTED")])
def test_publish_serializes_drafts_and_rechecks_stale_retired_state(engine, revision_case, target_index, expected):
    _, ids, _ = revision_case
    ready, proceed = threading.Event(), threading.Event()
    worker_pid = []

    def second_publisher():
        with Session(engine) as db, db.begin():
            db.execute(text("SET LOCAL lock_timeout = '8s'"))
            stale = db.get(Revision, ids[target_index])  # Cached before the competing publish.
            worker_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            assert proceed.wait(5)
            try:
                publish(db, stale.id)
                return stale.status
            except ValueError:
                return "REJECTED"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(second_publisher)
        assert ready.wait(5)
        with Session(engine) as first:
            first.execute(text("SET LOCAL lock_timeout = '8s'"))
            blocker = first.scalar(text("SELECT pg_backend_pid()"))
            publish(first, ids[1])
            proceed.set()
            wait_for_blocker(engine, worker_pid[0], blocker)
            first.commit()
        assert future.result(timeout=10) == expected
    with Session(engine) as db:
        states = [db.get(Revision, i).status for i in ids]
        assert states == (["RETIRED", "RETIRED", "PUBLISHED"] if target_index == 2
                          else ["RETIRED", "PUBLISHED", "DRAFT"])


@pytest.mark.parametrize("title", ["并发手册", "首次上传手册"])
def test_parallel_uploads_share_document_and_receive_unique_versions(engine, enc, revision_case, title):
    kb_id, _, path = revision_case
    start = threading.Barrier(2)

    def upload():
        with Session(engine) as db, db.begin():
            db.execute(text("SET LOCAL lock_timeout = '8s'"))
            start.wait(timeout=5)
            revision = add_revision(db, enc, kb_id=kb_id, title=title, lang="zh-Hans", path=path)
            return revision.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload) for _ in range(2)]
        versions = sorted(f.result(timeout=15) for f in futures)
    assert versions == ([4, 5] if title == "并发手册" else [1, 2])
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Document)
                         .where(Document.kb_id == kb_id, Document.title == title)) == 1


def test_first_upload_language_race_is_rechecked_after_encoding(engine, enc, revision_case):
    from knoventra.ingestion.service import DocumentMetadataError
    kb_id, _, path = revision_case
    gate = threading.Barrier(2)
    class GatedEncoder:
        def __init__(self):
            self.first = True
        def encode(self, texts):
            if self.first:
                self.first = False
                gate.wait(timeout=5)  # Both metadata preflights saw no existing document.
            return enc.encode(texts)
    def upload(lang):
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '8s'"))
                add_revision(session, GatedEncoder(), kb_id=kb_id, title='不同语言首次上传', lang=lang, path=path)
            return 'CREATED', lang
        except DocumentMetadataError:
            return 'REJECTED', lang
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload, lang) for lang in ('zh-Hans', 'en')]
        outcomes = [future.result(timeout=15) for future in futures]
    assert sorted(status for status, _ in outcomes) == ['CREATED', 'REJECTED']
    winner = next(lang for status, lang in outcomes if status == 'CREATED')
    with Session(engine) as session:
        doc = session.scalar(select(Document).where(Document.kb_id == kb_id, Document.title == '不同语言首次上传'))
        assert doc.source_lang == winner
        revisions = session.scalars(select(Revision).where(Revision.document_id == doc.id)).all()
        assert len(revisions) == 1 and revisions[0].status == 'DRAFT'


def test_database_rejects_two_published_revisions_even_when_bypassing_service(db):
    doc = db.scalar(select(Document).where(Document.title.like("E17%")))
    draft = db.scalar(select(Revision).where(Revision.document_id == doc.id, Revision.status == "DRAFT"))
    with pytest.raises(IntegrityError), db.begin_nested():
        draft.status = "PUBLISHED"
        db.flush()


def test_publish_retry_preserves_publication_time(db):
    rev = db.scalar(select(Revision).where(Revision.status == "PUBLISHED"))
    before = rev.published_at
    publish(db, rev.id)
    assert rev.status == "PUBLISHED" and rev.published_at == before


def test_empty_revision_cannot_retire_previous_version(db):
    doc = db.scalar(select(Document).where(Document.title.like("E17%")))
    old = db.scalar(select(Revision).where(Revision.document_id == doc.id, Revision.status == "PUBLISHED"))
    empty = Revision(document_id=doc.id, version=99, status="DRAFT", content_hash="empty")
    db.add(empty)
    db.flush()
    with pytest.raises(ValueError, match="no chunks"):
        publish(db, empty.id)
    assert old.status == "PUBLISHED" and empty.status == "DRAFT"


@pytest.mark.parametrize("body", ["", "# 只有标题\n## 没有正文\n"])
def test_empty_document_is_rejected_before_encoding_or_mutation(db, enc, tmp_path, body):
    path = tmp_path / "empty.md"
    path.write_text(body, encoding="utf-8")
    kb = db.scalar(select(KnowledgeBase))
    with pytest.raises(ValueError, match="no extractable text"):
        add_revision(db, enc, kb_id=kb.id, title="空文档", lang="zh-Hans", path=path)
    assert db.scalar(select(Document).where(Document.kb_id == kb.id, Document.title == "空文档")) is None


@pytest.mark.parametrize("bad_call", [1, 2])
def test_truncated_encoder_output_never_creates_a_draft(db, enc, tmp_path, bad_call):
    path = tmp_path / "manual.md"
    path.write_text("# 手册\n## 第一节\n正文一。\n## 第二节\n正文二。", encoding="utf-8")
    kb = db.scalar(select(KnowledgeBase))
    calls = 0

    class TruncatedEncoder:
        def encode(self, texts):
            nonlocal calls
            calls += 1
            rows = enc.encode(texts)
            return rows[:-1] if calls == bad_call else rows

    with pytest.raises(ValueError, match="count does not match"):
        add_revision(db, TruncatedEncoder(), kb_id=kb.id, title="编码失败文档", lang="zh-Hans", path=path)
    assert db.scalar(select(Document).where(Document.kb_id == kb.id, Document.title == "编码失败文档")) is None


def migration():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "alembic/versions/0004_revision_constraints.py"))


def test_revision_constraints_migration_roundtrip_preserves_data(db):
    revision = db.scalar(select(Revision).where(Revision.status == "PUBLISHED"))
    before = (revision.id, revision.status, revision.published_at)
    steps = migration()
    with Operations.context(MigrationContext.configure(db.connection())):
        steps["downgrade"]()
        steps["upgrade"]()
    db.refresh(revision)
    assert (revision.id, revision.status, revision.published_at) == before


@pytest.mark.parametrize("conflict", ["document", "published"])
def test_migration_rejects_conflicting_legacy_data_without_choosing_a_winner(db, conflict):
    doc = db.scalar(select(Document).where(Document.title.like("E17%")))
    steps = migration()
    with pytest.raises(IntegrityError), db.begin_nested():
        with Operations.context(MigrationContext.configure(db.connection())):
            steps["downgrade"]()
            if conflict == "document":
                db.add(Document(kb_id=doc.kb_id, title=doc.title, source_lang=doc.source_lang))
            else:
                draft = db.scalar(select(Revision).where(Revision.document_id == doc.id, Revision.status == "DRAFT"))
                draft.status = "PUBLISHED"
            db.flush()
            steps["upgrade"]()
    # Rolling back the failed migration leaves the initial data and constraints intact.
    assert db.scalar(select(func.count()).select_from(Revision)
                     .where(Revision.document_id == doc.id, Revision.status == "PUBLISHED")) == 1
