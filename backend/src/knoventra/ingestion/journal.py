"""Technical-journal template adapter and skill-matrix ingestion.

The template is one sheet with seven named headers. Daily journal rows and
skill-matrix rows share those columns; a skill row has no date and carries
"作业次数：N；掌握程度：X" in 工作内容. Columns are located by header name, so
column order and extra columns do not matter; a missing required header
rejects the whole sheet.

    uv run python -m knoventra.ingestion.journal ../data/demo/journals/*.xlsx --registry ../data/demo/journals/machines.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.db.models import Skill
from knoventra.ingestion.normalize import to_simplified

MACHINE, DATE, ENGINEER, TOPIC, CONTENT, NOTES, COMPANY = (
    "仪器编号", "日期", "技术员", "工作主题", "工作内容", "注意事项", "公司名称")
REQUIRED_HEADERS = (MACHINE, DATE, ENGINEER, TOPIC, CONTENT, NOTES, COMPANY)
HEADER_SCAN_ROWS = 10

SKILL_PATTERN = re.compile(r"作业次数\s*[:：]\s*(\d+)\s*[;；,，]\s*掌握程度\s*[:：]\s*(不了解|了解|掌握)")
LEVELS = {"不了解": "none", "了解": "familiar", "掌握": "proficient"}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S")


class JournalTemplateError(ValueError):
    """The sheet does not match the template; nothing from it is ingested."""

    def __init__(self, message: str, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = missing or []


@dataclass(frozen=True)
class SourceRef:
    file: str
    sheet: str
    row: int


@dataclass(frozen=True)
class JournalRecord:
    source: SourceRef
    machine: str
    date: dt.date
    engineer: str
    topic: str
    content: str
    notes: str
    company: str


@dataclass(frozen=True)
class SkillRecord:
    source: SourceRef
    machine: str
    engineer: str
    skill: str
    op_count: int
    level: str  # none | familiar | proficient
    note: str
    company: str


@dataclass(frozen=True)
class RejectedRow:
    source: SourceRef
    reason: str


@dataclass
class ParsedSheet:
    journals: list[JournalRecord] = field(default_factory=list)
    skills: list[SkillRecord] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)


def _norm_header(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("　", "")


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Instrument numbers typed as numbers come back as floats.
    return str(value).strip()


def _date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _cell(value)
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def locate_columns(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    """Return (header row index, {required header: column index})."""
    best: tuple[int, dict[str, int]] = (-1, {})
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        found: dict[str, int] = {}
        for j, value in enumerate(row):
            name = _norm_header(value)
            if name in REQUIRED_HEADERS and name not in found:
                found[name] = j
        if len(found) > len(best[1]):
            best = (i, found)
        if len(found) == len(REQUIRED_HEADERS):
            return i, found
    missing = [h for h in REQUIRED_HEADERS if h not in best[1]]
    raise JournalTemplateError(f"missing required headers: {', '.join(missing)}", missing)


def parse_rows(rows: list[tuple], *, file: str, sheet: str) -> ParsedSheet:
    header_at, cols = locate_columns(rows)
    out = ParsedSheet()
    for offset, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        ref = SourceRef(file, sheet, offset)
        get = {h: (row[j] if j < len(row) else None) for h, j in cols.items()}
        if all(_cell(v) == "" for v in row):
            continue
        machine, engineer, topic = _cell(get[MACHINE]), _cell(get[ENGINEER]), _cell(get[TOPIC])
        content, notes, company = _cell(get[CONTENT]), _cell(get[NOTES]), _cell(get[COMPANY])
        skill = SKILL_PATTERN.fullmatch(content)
        if skill and _cell(get[DATE]) == "":
            if not (machine and engineer and topic):
                out.rejected.append(RejectedRow(ref, "skill row needs machine, engineer and skill item"))
                continue
            out.skills.append(SkillRecord(ref, machine, engineer, topic, int(skill.group(1)),
                                          LEVELS[skill.group(2)], notes, company))
            continue
        date = _date(get[DATE])
        if date is None:
            out.rejected.append(RejectedRow(ref, "journal row has no valid date"))
            continue
        if not (machine and engineer):
            out.rejected.append(RejectedRow(ref, "journal row needs machine and engineer"))
            continue
        if not (topic or content):
            out.rejected.append(RejectedRow(ref, "journal row has no topic or content"))
            continue
        out.journals.append(JournalRecord(ref, machine, date, engineer, topic, content, notes, company))
    return out


def parse_workbook(path: Path) -> ParsedSheet:
    """Parse every sheet that matches the template. Fails if none does."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        result, errors = ParsedSheet(), []
        for ws in wb.worksheets:
            rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
            try:
                part = parse_rows(rows, file=path.name, sheet=ws.title)
            except JournalTemplateError as exc:
                errors.append(exc)
                continue
            result.journals += part.journals
            result.skills += part.skills
            result.rejected += part.rejected
        if errors and not (result.journals or result.skills or result.rejected):
            raise JournalTemplateError(str(errors[0]), errors[0].missing)
        return result
    finally:
        wb.close()


def skill_key(skill: str) -> str:
    """Case, width and script variants of one skill item share a key."""
    return re.sub(r"\s+", " ", to_simplified(skill).casefold()).strip()


def upsert_skills(db: Session, records: list[SkillRecord]) -> int:
    """Idempotent: one row per (machine, engineer, skill key); a re-import overwrites it."""
    from knoventra.ingestion.cases import redact

    latest: dict[tuple[str, str, str], SkillRecord] = {}
    for r in records:
        latest[(r.machine, r.engineer, skill_key(r.skill))] = r  # Later rows win within a batch.
    for (machine, engineer, key), r in latest.items():
        values = dict(machine_id=machine, engineer_name=engineer, skill_key=key, skill_item=r.skill,
                      op_count=r.op_count, level=r.level, note=redact(r.note)[0], company=r.company,
                      source_file=r.source.file, source_row=r.source.row)
        stmt = pg_insert(Skill).values(**values)
        db.execute(stmt.on_conflict_do_update(
            constraint="uq_knv_skill_machine_engineer_key",
            set_={k: stmt.excluded[k] for k in values if k not in ("machine_id", "engineer_name", "skill_key")}
            | {"updated_at": func.clock_timestamp()},
        ))
    db.flush()
    return len(latest)


def import_workbooks(db: Session, files: list[Path], registry_path: Path | None = None) -> dict:
    """Parse, redact, dedup and store one batch; skills, cases and the audit row commit together."""
    from knoventra.ingestion.cases import build_cases, load_registry, store_cases, write_audit

    parsed = ParsedSheet()
    for path in files:
        part = parse_workbook(path)
        parsed.journals += part.journals
        parsed.skills += part.skills
        parsed.rejected += part.rejected
    registry = load_registry(registry_path) if registry_path else {}
    drafts, stats = build_cases(parsed.journals, registry)
    skills = upsert_skills(db, parsed.skills)
    created, updated = store_cases(db, drafts, registry)
    counts = {"journal_rows": len(parsed.journals), "skill_rows": len(parsed.skills), "skills_stored": skills,
              "rejected": len(parsed.rejected), "exact_merged": stats.exact_merged,
              "near_merged": stats.near_merged, "unknown_model": stats.unknown_model,
              "cases_created": created, "cases_updated": updated}
    audit = write_audit(db, files=[p.name for p in files], counts=counts, stats=stats)
    return {"counts": counts, "redactions": audit.redactions,
            "rejected": [f"{r.source.file}:{r.source.sheet}:{r.source.row} {r.reason}" for r in parsed.rejected]}


def main() -> None:
    import json

    from knoventra.db.session import SessionLocal

    ap = argparse.ArgumentParser(description="Import journal workbooks: skill matrix and field cases.")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--registry", type=Path, help="machines.json asset registry (serial -> model)")
    args = ap.parse_args()
    with SessionLocal() as db, db.begin():
        report = import_workbooks(db, args.files, args.registry)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
