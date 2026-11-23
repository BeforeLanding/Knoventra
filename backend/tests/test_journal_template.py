"""Journal template adapter and the fictional journal generator (no database)."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from knoventra.ingestion.journal import (
    REQUIRED_HEADERS,
    JournalTemplateError,
    parse_rows,
    parse_workbook,
    skill_key,
)

REPO = Path(__file__).resolve().parents[2]
JOURNALS = REPO / "data" / "demo" / "journals"
HEADERS = list(REQUIRED_HEADERS)


def _generator():
    spec = importlib.util.spec_from_file_location("generate_journals", REPO / "data" / "demo" / "generate_journals.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _journal(machine="610101", date=dt.datetime(2026, 3, 2), engineer="Chen Lixing", topic="EMO 测试",
             content="1.逐个按下 EMO", notes="", company="NRS"):
    return [machine, date, engineer, topic, content, notes, company]


def test_columns_are_located_by_name_not_position():
    order = [6, 0, 3, 1, 5, 2, 4]
    headers = [HEADERS[i] for i in order] + ["备注（额外列）"]
    row = _journal()
    parsed = parse_rows([tuple(headers), tuple([row[i] for i in order] + ["ignored"])], file="f.xlsx", sheet="s")
    assert not parsed.rejected
    (rec,) = parsed.journals
    assert (rec.machine, rec.date, rec.engineer, rec.company) == ("610101", dt.date(2026, 3, 2), "Chen Lixing", "NRS")
    assert rec.source.row == 2


def test_header_row_may_follow_a_title_row_and_tolerates_spaces():
    title = ("2026 年技术日志",)
    headers = tuple(h if h != "技术员" else " 技 术 员 " for h in HEADERS)
    parsed = parse_rows([title, headers, tuple(_journal())], file="f.xlsx", sheet="s")
    assert len(parsed.journals) == 1 and parsed.journals[0].source.row == 3


def test_missing_required_header_rejects_the_whole_sheet():
    headers = [h for h in HEADERS if h not in ("日期", "公司名称")]
    with pytest.raises(JournalTemplateError) as exc:
        parse_rows([tuple(headers), ("x",) * len(headers)], file="f.xlsx", sheet="s")
    assert exc.value.missing == ["日期", "公司名称"]


def test_skill_rows_share_the_sheet_and_are_split_out():
    rows = [
        tuple(HEADERS),
        tuple(_journal()),
        ("610101", None, "Zhou Lan", "golden check", "作业次数：3；掌握程度：掌握", "", "NRS"),
        ("610101", None, "Zhou Lan", "EMO 测试", "作业次数: 0; 掌握程度: 不了解", "未实际操作", "NRS"),
    ]
    parsed = parse_rows(rows, file="f.xlsx", sheet="s")
    assert len(parsed.journals) == 1
    assert [(s.skill, s.op_count, s.level) for s in parsed.skills] == [
        ("golden check", 3, "proficient"), ("EMO 测试", 0, "none")]
    assert parsed.skills[1].note == "未实际操作"


def test_bad_rows_are_rejected_with_a_reason_and_blank_rows_skipped():
    rows = [
        tuple(HEADERS),
        tuple(_journal(date="not a date")),
        (None,) * 7,
        tuple(_journal(engineer=None)),
        tuple(_journal(topic=None, content=None)),
        ("610101", None, None, "golden check", "作业次数：1；掌握程度：了解", None, "NRS"),
        tuple(_journal(date="2026/03/09", machine=610101.0)),
    ]
    parsed = parse_rows(rows, file="f.xlsx", sheet="s")
    assert [(r.source.row, r.reason.split()[0]) for r in parsed.rejected] == [
        (2, "journal"), (4, "journal"), (5, "journal"), (6, "skill")]
    (ok,) = parsed.journals
    assert ok.date == dt.date(2026, 3, 9) and ok.machine == "610101"


def test_skill_key_merges_case_spacing_and_script_variants():
    assert skill_key("Golden  Check ") == skill_key("golden check")
    assert skill_key("EMO 測試") == skill_key("emo 测试")


def test_generator_is_deterministic_and_labels_every_difficulty():
    gen = _generator()
    a, b = gen.build(), gen.build()
    assert gen.fingerprint(a) == gen.fingerprint(b)
    assert gen.fingerprint(gen.build(seed=1)) != gen.fingerprint(a)
    manifest = json.loads((JOURNALS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_sha256"] == gen.fingerprint(a), "regenerate data/demo/journals"
    gen.check(manifest)


def test_committed_workbooks_parse_cleanly_and_match_the_manifest():
    manifest = json.loads((JOURNALS / "manifest.json").read_text(encoding="utf-8"))
    journals, skills = 0, 0
    for f in manifest["files"]:
        parsed = parse_workbook(JOURNALS / f["file"])
        assert not parsed.rejected
        assert len(parsed.skills) == f["skill_rows"]
        journals += len(parsed.journals)
        skills += len(parsed.skills)
    assert (journals, skills) == (manifest["journal_rows"], manifest["skill_rows"])
    labelled = {(r["file"], r["row"]) for r in manifest["rows"]}
    assert len(labelled) == manifest["journal_rows"]


def test_workbook_without_a_template_sheet_is_rejected(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["仪器编号", "技术员"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    with pytest.raises(JournalTemplateError) as exc:
        parse_workbook(path)
    assert "日期" in exc.value.missing
