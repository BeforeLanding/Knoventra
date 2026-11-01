"""Load the fictional demo company: users, KBs, grants, documents.

    uv run python -m knoventra.db.seed [--manifest data/demo/manifest.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.auth.security import hash_password
from knoventra.db.models import Document, KbGrant, KnowledgeBase, Revision, User
from knoventra.ingestion.encoder import Encoder, get_encoder
from knoventra.ingestion.service import add_revision, publish

DEFAULT_MANIFEST = Path(__file__).resolve().parents[4] / "data" / "demo" / "manifest.json"


def seed(db: Session, enc: Encoder, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, list[str]]:
    """Idempotent. Returns {doc title: [revision ids by version]} for tests/CLI."""
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent / "docs"
    pw = hash_password(m["password"])
    for u in m["users"]:
        if not db.scalar(select(User).where(User.email == u["email"])):
            db.add(User(password_hash=pw, **u))
    kb_ids: dict[str, str] = {}
    for k in m["kbs"]:
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == k["name"]))
        if kb is None:
            kb = KnowledgeBase(name=k["name"], description=k["description"])
            db.add(kb)
            db.flush()
            for g in k["grants"]:
                db.add(KbGrant(kb_id=kb.id, region=g.get("region"), department=g.get("department"),
                               role=g.get("role")))
        kb_ids[k["name"]] = kb.id
    db.flush()
    out: dict[str, list[str]] = {}
    for d in m["documents"]:
        kb_id = kb_ids[d["kb"]]
        doc = db.scalar(select(Document).where(Document.kb_id == kb_id, Document.title == d["title"]))
        if doc is not None:
            continue
        revs = [add_revision(db, enc, kb_id=kb_id, title=d["title"], lang=d["lang"], path=base / f).id
                for f in d["files"]]
        publish(db, revs[d["publish"] - 1])
        out[d["title"]] = revs
    db.flush()
    return out


def main() -> None:
    from knoventra.db.session import SessionLocal

    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = ap.parse_args()
    with SessionLocal.begin() as db:
        r = seed(db, get_encoder(), args.manifest)
    print(f"seeded {len(r)} documents")


if __name__ == "__main__":
    main()
