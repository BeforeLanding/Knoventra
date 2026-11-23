"""Who can do a skill: structured lookup over knv_skill (never RAG).

Matching is token based on the normalised skill key, expanded with the same
synonym table the ES index uses, so "chamber 示教" finds "chamber teaching".
Only skills with the best overlap are returned; engineers who do not know a
skill are left out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.db.models import Skill, User
from knoventra.ingestion.journal import skill_key

LEVEL_ORDER = {"proficient": 0, "familiar": 1}
LEVEL_LABEL = {"proficient": "掌握", "familiar": "了解"}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]*|[一-鿿]+")


@dataclass(frozen=True)
class SkillHit:
    skill_item: str
    engineer: str
    level: str
    op_count: int
    machine_id: str


def can_view_skills(user: User) -> bool:
    """The skill matrix is Ops staff data: Ops users and admins only."""
    return user.department == "Ops" or user.role == "admin"


@lru_cache(maxsize=1)
def _synonyms() -> dict[str, set[str]]:
    path = Path(__file__).resolve().parent / "search_es" / "synonyms.txt"
    groups: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        members = {t for term in line.split(",") for t in _TOKEN.findall(skill_key(term))}
        for m in members:
            groups.setdefault(m, set()).update(members)
    return groups


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(skill_key(text)))


def _score(query_key: str, query_tokens: set[str], skill: str) -> int:
    score = 0
    for t in _tokens(skill):
        if t in query_tokens or (len(t) >= 2 and not t.isascii() and t in query_key):
            score += 1
    return score


def lookup_skills(db: Session, query: str, limit: int = 8) -> list[SkillHit]:
    from knoventra.agent.query_plan import fix_spelling  # same typo table as the ES index

    query = fix_spelling(query)
    query_key = skill_key(query)
    tokens = _tokens(query)
    for t in list(tokens):
        tokens |= _synonyms().get(t, set())
    expanded_key = query_key + " " + " ".join(sorted(tokens))
    rows = db.scalars(select(Skill).where(Skill.level != "none")).all()
    scored = [(s, _score(expanded_key, tokens, s.skill_item)) for s in rows]
    best = max((score for _, score in scored), default=0)
    if best == 0:
        return []
    hits = [SkillHit(s.skill_item, s.engineer_name, s.level, s.op_count, s.machine_id)
            for s, score in scored if score == best]
    hits.sort(key=lambda h: (LEVEL_ORDER[h.level], -h.op_count, h.engineer, h.machine_id))
    return hits[:limit]


def format_hits(hits: list[SkillHit]) -> str:
    if not hits:
        return "技能表中没有找到相关技能，或没有人掌握该技能。"
    return "\n".join(f"{h.skill_item}：{h.engineer}（{LEVEL_LABEL[h.level]}，作业 {h.op_count} 次，机台 {h.machine_id}）"
                     for h in hits)
