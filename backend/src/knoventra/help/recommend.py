"""Expert recommendation for a field help request.

Candidates are engineers whose skill-matrix level is "proficient" for the skill
the request is about (matched with the same tokens and synonyms as
lookup_skills) and who have an enabled login. Order: same region as the
requester, fewer open assigned requests, more recorded practice, name. With no
candidate the request stays OPEN in the dispatch queue.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knoventra.db.models import HelpRequest, User
from knoventra.skills import lookup_skills

OPEN_LOAD_STATUSES = ("ASSIGNED", "IN_PROGRESS")


@dataclass(frozen=True)
class Candidate:
    user_id: str
    email: str
    engineer: str
    skill: str
    op_count: int
    same_region: bool
    open_load: int

    @property
    def reason(self) -> str:
        where = "同区域" if self.same_region else "跨区域"
        return f"技能“{self.skill}”为掌握（作业 {self.op_count} 次）；{where}；当前未结 {self.open_load} 单"


@dataclass(frozen=True)
class Recommendation:
    chosen: Candidate | None
    candidates: list[Candidate]
    reason: str

    def as_dict(self) -> dict:
        return {"assignee_id": self.chosen.user_id if self.chosen else None,
                "assignee": self.chosen.engineer if self.chosen else None, "reason": self.reason}


def recommend(db: Session, requester: User, text: str) -> Recommendation:
    hits = [h for h in lookup_skills(db, text, limit=50) if h.level == "proficient"]
    if not hits:
        return Recommendation(None, [], "技能表中没有找到掌握相关技能的工程师，进入待派单队列")
    best: dict[str, tuple[str, int]] = {}
    for h in hits:  # one entry per engineer: their highest practice count across machines
        if h.engineer not in best or h.op_count > best[h.engineer][1]:
            best[h.engineer] = (h.skill_item, h.op_count)
    users = db.scalars(select(User).where(User.engineer_name.in_(best), User.enabled.is_(True))).all()
    loads = dict(db.execute(select(HelpRequest.assignee_id, func.count())
                            .where(HelpRequest.assignee_id.in_([u.id for u in users]),
                                   HelpRequest.status.in_(OPEN_LOAD_STATUSES))
                            .group_by(HelpRequest.assignee_id)).all())
    candidates = sorted(
        (Candidate(u.id, u.email, u.engineer_name, best[u.engineer_name][0], best[u.engineer_name][1],
                   requester.region is not None and u.region == requester.region, loads.get(u.id, 0))
         for u in users if u.id != requester.id),
        key=lambda c: (not c.same_region, c.open_load, -c.op_count, c.engineer))
    if not candidates:
        return Recommendation(None, [], "掌握相关技能的工程师没有可用账号，进入待派单队列")
    return Recommendation(candidates[0], candidates, candidates[0].reason)
