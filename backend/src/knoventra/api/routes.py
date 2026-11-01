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
from knoventra.ingestion.encoder import get_encoder
from knoventra.ingestion.service import add_revision, publish
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
            "department": user.department, "role": user.role}


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
    return {"evidence": [e.__dict__ for e in res.evidence], "stages": res.stages}


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
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / f"upload{suffix}"
        p.write_bytes(file.file.read())
        rev = add_revision(db, get_encoder(), kb_id=kb_row.id, title=title, lang=lang, path=p)
    db.commit()
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
    kbs = [k.name for k in db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name))]
    docs = []
    for d, kb in db.execute(select(Document, KnowledgeBase).join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                            .order_by(KnowledgeBase.name, Document.title)):
        revs = db.scalars(select(Revision).where(Revision.document_id == d.id).order_by(Revision.version)).all()
        docs.append({"kb": kb.name, "title": d.title, "lang": d.source_lang,
                     "revisions": [{"id": r.id, "version": r.version, "status": r.status,
                                    "published_at": r.published_at} for r in revs]})
    return {"kbs": kbs, "documents": docs}
