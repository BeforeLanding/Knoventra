"""Machine-model handling: what the user stated, and when an answer depends on the model.

A document is "model-sectioned" when its published chunks carry at least two
different model sets and at least one chunk names a single model (e.g. a
spindle SOP with separate WX-38 and WX-P sections). Field cases each name one
model, so they never trigger a clarification on their own.
"""
from __future__ import annotations

from langchain_core.messages import AnyMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.db.models import Chunk, Revision
from knoventra.ingestion.cases import models_from_text


def chunk_models(section_path: str, text: str) -> frozenset[str]:
    return frozenset(models_from_text(f"{section_path}\n{text}"))


def stated_models(messages: list[AnyMessage]) -> list[str]:
    """Models named in the most recent user message that names any (later turns override earlier ones)."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            named = models_from_text(m.content if isinstance(m.content, str) else str(m.content))
            if named:
                return named
    return []


def sectioned_documents(db: Session, document_ids: set[str]) -> dict[str, list[str]]:
    """{document_id: models it has separate sections for} for model-sectioned documents only."""
    if not document_ids:
        return {}
    rows = db.execute(select(Revision.document_id, Chunk.section_path, Chunk.text)
                      .join(Chunk, Chunk.revision_id == Revision.id)
                      .where(Revision.document_id.in_(document_ids), Revision.status == "PUBLISHED")).all()
    sets: dict[str, set[frozenset[str]]] = {}
    for doc_id, section, text in rows:
        models = chunk_models(section, text)
        if models:
            sets.setdefault(doc_id, set()).add(models)
    return {doc: sorted(set().union(*s)) for doc, s in sets.items()
            if len(s) >= 2 and any(len(m) == 1 for m in s)}


def clarification_needed(db: Session, evidence, stated: list[str]) -> dict | None:
    """Ask for the model when the user named none and an answer comes from a model-sectioned document."""
    if stated:
        return None
    docs = {e.document_id for e in evidence if chunk_models(e.section_path, e.text)}
    sectioned = sectioned_documents(db, docs)
    for e in evidence:
        if e.document_id in sectioned:
            return {"document_title": e.document_title, "models": sectioned[e.document_id]}
    return None


def clarification_text(c: dict) -> str:
    return (f"这个操作在不同机型上的做法不一样：《{c['document_title']}》分别写了 {'、'.join(c['models'])}。"
            "请问你的机台是哪个机型？")
