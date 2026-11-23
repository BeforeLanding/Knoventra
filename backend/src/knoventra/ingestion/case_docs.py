"""Publish deduplicated field cases as knowledge-base documents.

Each `knv_journal_case` becomes one Markdown document so it goes through the
normal revision, permission, chunking, pgvector and ES paths. Re-running is
idempotent: unchanged cases are skipped, changed ones get a new revision.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.db.models import Document, JournalCase, Revision
from knoventra.ingestion.encoder import Encoder
from knoventra.ingestion.service import TITLE_MAX_CHARS, add_revision, publish

RESOLVED = {True: "已解决", False: "未解决（待跟踪）", None: "未知"}


def case_title(case: JournalCase) -> str:
    return f"CASE-{case.text_key[:8].upper()} {case.topic}"[:TITLE_MAX_CHARS]


def render_case(case: JournalCase) -> str:
    """One heading only: a case is short and must stay one chunk, so the symptom, the
    fix and the applicable model are always retrieved together."""
    models = "、".join(case.models) or "未知"
    lines = [f"# {case_title(case)}", "", f"适用机型：{models}"]
    if case.category == "troubleshooting":
        lines.append(f"故障现象：{case.symptom or case.topic}")
        if case.alarm_codes:
            lines.append(f"告警码：{'、'.join(case.alarm_codes)}")
    lines.append(f"处理记录：{case.content or '（无）'}")
    if case.notes:
        lines.append(f"注意事项：{case.notes}")
    if case.category == "troubleshooting":
        lines.append(f"处理结果：{RESOLVED[case.resolved]}")
        if case.cause:
            lines.append(f"原因：{case.cause}")
        if case.solution:
            lines.append(f"解决方法：{case.solution}")
    lines.append(f"来源：机台 {'、'.join(case.machine_ids)}；记录 {case.occurrences} 条；"
                 f"日期 {case.first_date.isoformat()} 至 {case.last_date.isoformat()}")
    return "\n".join(lines) + "\n"


def publish_cases(db: Session, enc: Encoder, kb_id: str, cases: list[JournalCase] | None = None) -> dict[str, int]:
    """Create or update one document per case in `kb_id`. Returns counts."""
    cases = cases if cases is not None else list(db.scalars(select(JournalCase).order_by(JournalCase.first_date,
                                                                                         JournalCase.id)))
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for case in cases:
            body = render_case(case)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            title = case_title(case)
            current = db.scalar(select(Revision.content_hash).join(Document, Document.id == Revision.document_id)
                                .where(Document.kb_id == kb_id, Document.title == title,
                                       Revision.status == "PUBLISHED"))
            if current == digest:
                counts["unchanged"] += 1
                continue
            path = Path(tmp) / f"{case.id}.md"
            path.write_bytes(body.encode("utf-8"))  # exact bytes: the revision hash must equal `digest`
            rev = add_revision(db, enc, kb_id=kb_id, title=title, lang=case.lang, path=path)
            publish(db, rev.id)
            counts["updated" if current else "created"] += 1
    return counts
