import copy
import json
import sys

import pytest

from answer_review import make_snapshot, prepare_packet, split_claims, summarize_packet, validate_snapshot
from review_answers import main


def card(alias="S1", text="先断电，清理进料口，重新上电。"):
    return {"alias": alias, "chunk_id": "chunk-" + alias, "document_id": "manual", "document_title": "设备手册",
            "revision": 1, "page": 2, "section_path": "E17-07 > 排查", "text": text, "lang": "zh-Hans"}


def report(answer="先断电 [S1]。清理进料口 [S1]。", *, error=None):
    c = card()
    return {"mode": "agent", "cases": [{"id": "test-01", "answer": answer,
            "review_snapshot": make_snapshot("E17-07 怎么处理", answer, {"S1": c}, [c], error)}]}


def annotate(claim, *, kind="human", verdict="supported", alias="S1", quote="先断电"):
    claim.update(verdict=verdict, reviewer="test-reviewer", reviewer_kind=kind, notes="用来源原文逐项核对。",
                 supports=[{"alias": alias, "quote": quote}] if quote is not None else [])


def test_snapshot_is_detached_and_source_drift_is_detected():
    c = card()
    snapshot = make_snapshot("q", "先断电 [S1]。", {"S1": c}, [c], None)
    c["text"] = "后来更新了手册。"
    assert validate_snapshot(snapshot)["S1"]["text"] == "先断电，清理进料口，重新上电。"
    snapshot["evidence"][0]["revision"] = 2
    with pytest.raises(ValueError, match="checksum"):
        validate_snapshot(snapshot)


def test_renamed_or_duplicate_aliases_and_mismatched_sources_cannot_be_captured():
    c = card()
    with pytest.raises(ValueError, match="registry keys"):
        make_snapshot("q", "a", {"S2": c}, [c], None)
    with pytest.raises(ValueError, match="cited source"):
        make_snapshot("q", "a", {"S1": c}, [card(text="另一个版本的正文")], None)


def test_sentence_units_keep_trailing_citations_and_preserve_traditional_glyphs():
    answer = "## 排查\n1. 執行零點校正。 [S1][S2]\n2. 檢查液位 35.5 mm [S1]。\n[S1]"
    units = split_claims(answer)
    assert [c["text"] for c in units] == ["## 排查", "1. 執行零點校正。 [S1][S2]", "2. 檢查液位 35.5 mm [S1]。"]
    assert units[1]["citations"] == ["S1", "S2"]


def test_unreviewed_packet_has_no_success_rate():
    r = report()
    p = prepare_packet(r)
    summary = summarize_packet(p, r)
    assert summary["claim_units"] == summary["unreviewed_units"] == 2
    assert summary["annotation_coverage"] == 0
    assert all(m["fully_supported_rate"] is None for m in summary["by_reviewer_kind"].values())


def test_human_and_agent_annotations_are_separate_and_pending_claims_are_visible():
    r = report("先断电 [S1]。应该持续等待 90 分钟 [S1]。请联系 Ops。")
    p = prepare_packet(r)
    claims = p["cases"][0]["claims"]
    annotate(claims[0])
    annotate(claims[1], kind="agent", verdict="unsupported", quote=None)
    result = summarize_packet(p, r)
    assert result["unreviewed_units"] == 1 and result["annotation_coverage"] == pytest.approx(0.6667)
    assert result["by_reviewer_kind"]["human"]["fully_supported_rate"] == 1.0
    assert result["by_reviewer_kind"]["agent"]["fully_supported_rate"] == 0.0


@pytest.mark.parametrize("alias, quote", [("S9", "先断电"), ("S1", "等待 90 分钟"), ("S1", ""), ("S1", None)])
def test_supported_claim_requires_a_real_captured_quote(alias, quote):
    r = report()
    p = prepare_packet(r)
    annotate(p["cases"][0]["claims"][0], alias=alias, quote=quote)
    with pytest.raises(ValueError):
        summarize_packet(p, r)


def test_source_quote_cannot_be_substituted_for_a_different_displayed_citation():
    r = report()
    row = r["cases"][0]
    one, two = card(), card("S2", "保持通风。")
    row["review_snapshot"] = make_snapshot("q", row["answer"], {"S1": one, "S2": two}, [one, two], None)
    p = prepare_packet(r)
    annotate(p["cases"][0]["claims"][0], alias="S2", quote="保持通风")
    with pytest.raises(ValueError, match="displayed citations"):
        summarize_packet(p, r)


def test_uncited_but_supported_claim_is_counted_as_missing_a_citation():
    r = report("先断电。")
    p = prepare_packet(r)
    annotate(p["cases"][0]["claims"][0])
    result = summarize_packet(p, r)
    assert result["uncited_reviewed_factual_claims"] == 1
    assert result["by_reviewer_kind"]["human"]["uncited_factual_claims"] == 1


@pytest.mark.parametrize("field, value", [("reviewer", None), ("reviewer_kind", "llm-as-human"), ("notes", "")])
def test_reviewed_claims_require_attribution_and_a_rationale(field, value):
    r = report()
    p = prepare_packet(r)
    annotate(p["cases"][0]["claims"][0])
    p["cases"][0]["claims"][0][field] = value
    with pytest.raises(ValueError):
        summarize_packet(p, r)


@pytest.mark.parametrize("mutation", ["claim", "citation", "snapshot", "report", "drop_claim", "drop_case"])
def test_packet_or_report_drift_cannot_be_silently_accepted(mutation):
    r = report()
    p = prepare_packet(r)
    if mutation == "claim":
        p["cases"][0]["claims"][0]["text"] = "一个不同的论断。"
    elif mutation == "citation":
        p["cases"][0]["claims"][0]["citations"] = ["S2"]
    elif mutation == "snapshot":
        p["cases"][0]["snapshot"]["sources"][0]["revision"] = 2
    elif mutation == "report":
        r["metadata"] = "changed"
    elif mutation == "drop_claim":
        p["cases"][0]["claims"].pop()
    else:
        p["cases"] = []
    with pytest.raises(ValueError):
        summarize_packet(p, r)


def test_old_report_is_rejected_and_refusal_has_explicit_empty_snapshot():
    with pytest.raises(ValueError, match="historical sources cannot be reconstructed"):
        prepare_packet({"mode": "agent", "cases": [{"id": "old", "answer": "旧答案 [S1]。"}]})
    snapshot = make_snapshot("不存在的故障码", "没有可靠依据。", {}, [], "INSUFFICIENT_EVIDENCE")
    assert validate_snapshot(snapshot) == {} and snapshot["sources"] == []


def test_case_selection_is_explicit_and_non_claims_do_not_inflate_support_rate():
    r = report("请联系 Ops。")
    second = copy.deepcopy(r["cases"][0])
    second["id"] = "test-02"
    r["cases"].append(second)
    p = prepare_packet(r, ["test-02"])
    annotate(p["cases"][0]["claims"][0], verdict="not_a_claim", quote=None)
    result = summarize_packet(p, r)
    assert result["report_cases"] == 2 and result["n_cases"] == 1
    assert result["selected_case_ids"] == ["test-02"]
    assert result["by_reviewer_kind"]["human"]["fully_supported_rate"] is None


def test_cli_round_trip_and_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    r = report()
    source, packet, output = [tmp_path / name for name in ("result.json", "review.json", "summary.json")]
    source.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["review_answers", "prepare", str(source), "--out", str(packet)])
    main()
    data = json.loads(packet.read_text(encoding="utf-8"))
    annotate(data["cases"][0]["claims"][0], kind="agent")
    packet.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["review_answers", "summarize", str(packet), "--report", str(source), "--out", str(output)])
    main()
    before = output.read_bytes()
    assert json.loads(before)["by_reviewer_kind"]["agent"]["supported"] == 1
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and output.read_bytes() == before
