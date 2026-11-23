"""Skill-matrix import into knv_skill."""
from sqlalchemy import func, select

from knoventra.db.models import Skill
from knoventra.ingestion.journal import SkillRecord, SourceRef, upsert_skills


def _rec(skill="golden check", count=1, level="familiar", engineer="Zhou Lan", row=10):
    return SkillRecord(SourceRef("journals-CN.xlsx", "工作周报", row), "610101", engineer, skill,
                       count, level, "", "NRS")


def _rows(db):
    return db.scalars(select(Skill).order_by(Skill.engineer_name, Skill.skill_key)).all()


def test_reimport_is_idempotent_and_updates_in_place(db):
    assert upsert_skills(db, [_rec(), _rec(engineer="Chen Lixing")]) == 2
    first_ids = [s.id for s in _rows(db)]
    upsert_skills(db, [_rec(), _rec(engineer="Chen Lixing")])
    assert db.scalar(select(func.count()).select_from(Skill)) == 2

    upsert_skills(db, [_rec(skill="Golden Check", count=4, level="proficient", row=42)])
    db.expire_all()
    rows = _rows(db)
    assert [s.id for s in rows] == first_ids
    zhou = next(s for s in rows if s.engineer_name == "Zhou Lan")
    assert (zhou.op_count, zhou.level, zhou.skill_item, zhou.source_row) == (4, "proficient", "Golden Check", 42)


def test_later_row_wins_within_one_batch(db):
    assert upsert_skills(db, [_rec(count=1, row=5), _rec(skill="GOLDEN CHECK", count=2, row=9)]) == 1
    (only,) = _rows(db)
    assert (only.op_count, only.source_row) == (2, 9)


def test_demo_workbooks_import_end_to_end(db):
    from pathlib import Path

    from knoventra.ingestion.journal import parse_workbook

    journals = Path(__file__).resolve().parents[2] / "data" / "demo" / "journals"
    records = [s for p in sorted(journals.glob("*.xlsx")) for s in parse_workbook(p).skills]
    stored = upsert_skills(db, records)
    assert stored == len(records) == db.scalar(select(func.count()).select_from(Skill))
    proficient = db.scalars(select(Skill.engineer_name).where(Skill.level == "proficient")).all()
    assert proficient, "demo data should name at least one proficient engineer"
