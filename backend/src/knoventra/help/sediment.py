"""Resolved field help becomes a case draft for review (D10).

On RESOLVED, a DRAFT revision is written to the `field-cases` knowledge base in
the same transaction. Nothing is searchable until an admin reviews and
publishes it through the normal revision workflow, which also queues the ES
sync. Text is redacted before it is written; reopening and resolving again adds
a new draft revision to the same document.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.db.models import Document, HelpEvent, HelpRequest, KnowledgeBase, Revision, User
from knoventra.ingestion.cases import detect_lang, redact
from knoventra.ingestion.encoder import Encoder
from knoventra.ingestion.service import TITLE_MAX_CHARS, add_revision

CASE_KB = "field-cases"


def case_title(req: HelpRequest) -> str:
    # Titles are stored, indexed and shown on source cards: redact them like the body.
    return redact(f"HELP-{req.id[:16].upper()} {req.symptom or req.summary.splitlines()[0]}")[0][:TITLE_MAX_CHARS]


def render(db: Session, req: HelpRequest) -> str:
    progress = [e.note for e in db.scalars(select(HelpEvent).where(HelpEvent.request_id == req.id,
                                                                   HelpEvent.action == "progress")
                                           .order_by(HelpEvent.at, HelpEvent.id)) if e.note]
    solver = db.get(User, req.assignee_id) if req.assignee_id else None
    lines = [f"# {case_title(req)}", "",
             f"适用机型：{req.machine_model or '未知'}",
             f"机台：{req.machine_id or '未知'}",
             f"故障现象：{req.symptom or req.summary}"]
    if req.alarm_codes:
        lines.append(f"告警码：{'、'.join(req.alarm_codes)}")
    if req.tried_steps:
        lines.append(f"已尝试：{req.tried_steps}")
    if progress:
        lines.append("处理过程：" + "；".join(progress))
    lines.append(f"解决方法：{req.resolution}")
    lines.append("处理结果：已解决")
    lines.append(f"来源：求助单 {req.id}；解决人 {solver.engineer_name or solver.email if solver else '未知'}；"
                 f"解决于 {req.resolved_at.date().isoformat() if req.resolved_at else '未知'}")
    text, _ = redact("\n".join(lines) + "\n")
    return text


def draft_case(db: Session, enc: Encoder, req: HelpRequest) -> Revision | None:
    """Write a DRAFT case revision for a resolved field help request; None when not applicable."""
    if req.kind != "field_help" or req.status != "RESOLVED" or not req.resolution:
        return None
    kb_id = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == CASE_KB))
    if kb_id is None:
        return None
    body = render(db, req)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{req.id}.md"
        path.write_bytes(body.encode("utf-8"))
        existing = db.scalar(select(Document.source_lang).where(Document.kb_id == kb_id,
                                                                Document.title == case_title(req)))
        rev = add_revision(db, enc, kb_id=kb_id, title=case_title(req), lang=existing or detect_lang(body), path=path)
    req.case_document_id = rev.document_id
    db.add(HelpEvent(request_id=req.id, actor_id=None, action="case_draft", from_status="RESOLVED",
                     to_status="RESOLVED", note=f"已生成案例草稿 v{rev.version}，等待审核发布"))
    db.flush()
    return rev
