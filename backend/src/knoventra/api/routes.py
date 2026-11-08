import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.access.scope import NoAccess, compute_scope
from knoventra.api.deps import current_user, get_db, require_admin
from knoventra.auth.security import issue_token, verify_password
from knoventra.config import settings
from knoventra.db.models import Document, KnowledgeBase, Revision, User
from knoventra.ingestion.chunking import DocumentParseError
from knoventra.ingestion.encoder import EncoderOutputError, get_encoder
from knoventra.ingestion.service import (
    SUPPORTED_LANGUAGES,
    TITLE_MAX_CHARS,
    DocumentMetadataError,
    add_revision,
    publish,
    validate_revision_metadata,
)
from knoventra.ingestion.uploads import UploadTooLarge, copy_upload
from knoventra.retrieval.search import search

router = APIRouter(prefix="/api/v1")


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))
    if not user or not user.enabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "bad credentials")
    return {"token": issue_token(user.id)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "region": user.region,
            "department": user.department, "role": user.role, "chat_max_chars": settings.chat_max_chars}


@router.get("/debug/search")
def debug_search(q: str, sparse: bool = True, opencc: bool = True,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Per-stage candidates. Bound by the same AccessScope as real retrieval."""
    if settings.env != "dev":
        raise HTTPException(404)
    try:
        scope = compute_scope(db, user)
    except NoAccess:
        return {"error": "NO_ACCESS", "evidence": [], "stages": {}}
    res = search(db, get_encoder(), scope, q, use_sparse=sparse, use_opencc=opencc)
    return {"evidence": [e.__dict__ for e in res.evidence], "stages": res.stages,
            "identifiers": {"required": res.required_identifiers, "missing": res.missing_identifiers}}


@router.post("/admin/documents")
def upload_document(kb: str = Form(...), title: str = Form(...), lang: str = Form(...),
                    file: UploadFile = File(...), _: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    kb_row = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb))
    if kb_row is None:
        raise HTTPException(404, "kb not found")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".md", ".pdf"}:
        raise HTTPException(400, "only .md and .pdf")
    try:
        validate_revision_metadata(db, kb_id=kb_row.id, title=title, lang=lang)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"upload{suffix}"
            copy_upload(file.file, p, settings.upload_max_bytes)
            rev = add_revision(db, get_encoder(), kb_id=kb_row.id, title=title, lang=lang, path=p)
        db.commit()
    except EncoderOutputError:
        db.rollback()
        raise HTTPException(503, "ENCODER_OUTPUT_INVALID") from None
    except DocumentParseError as error:
        db.rollback()
        raise HTTPException(422, {"code": "DOCUMENT_PARSE_INVALID", "message": str(error)}) from None
    except DocumentMetadataError as error:
        db.rollback()
        raise HTTPException(422, {"code": "INVALID_DOCUMENT_METADATA", "message": str(error)}) from None
    except UploadTooLarge:
        db.rollback()
        raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "max_bytes": settings.upload_max_bytes}) from None
    except ValueError as e:
        db.rollback()
        raise HTTPException(422, str(e)) from None
    return {"revision_id": rev.id, "version": rev.version, "status": rev.status}


@router.post("/admin/revisions/{revision_id}/publish")
def publish_revision(revision_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        publish(db, revision_id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e)) from None
    rev = db.get(Revision, revision_id)
    return {"revision_id": rev.id, "status": rev.status}


@router.get("/admin/documents")
def list_documents(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    kbs = list(db.scalars(select(KnowledgeBase.name).order_by(KnowledgeBase.name)))
    # Read only catalog fields, with a left join so revisionless documents stay
    # visible. The number of reads does not grow with the number of documents.
    rows = db.execute(select(
        Document.id, KnowledgeBase.name.label("kb"), Document.title, Document.source_lang,
        Revision.id.label("revision_id"), Revision.version, Revision.status, Revision.published_at,
    ).join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
     .outerjoin(Revision, Revision.document_id == Document.id)
     .order_by(KnowledgeBase.name, Document.title, Document.id, Revision.version, Revision.id))
    docs = {}
    for row in rows:
        doc = docs.setdefault(row.id, {"kb": row.kb, "title": row.title, "lang": row.source_lang,
                                      "revisions": []})
        if row.revision_id is not None:
            doc["revisions"].append({"id": row.revision_id, "version": row.version, "status": row.status,
                                     "published_at": row.published_at})
    return {"kbs": kbs, "documents": list(docs.values()), "upload_max_bytes": settings.upload_max_bytes,
            "upload_rules": {"title_max_chars": TITLE_MAX_CHARS, "languages": list(SUPPORTED_LANGUAGES)}}
