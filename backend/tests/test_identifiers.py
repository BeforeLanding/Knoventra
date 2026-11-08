"""Offline policy tests: no database, encoder model or LLM required."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knoventra.access.scope import AccessScope, NoAccess
from knoventra.retrieval import pipeline
from knoventra.retrieval.identifiers import evidence_identifiers, extract_identifiers, normalize_identifiers
from knoventra.retrieval.identifiers import extract_query_identifiers, normalize_query_identifiers
from knoventra.retrieval.search import Evidence, SearchResult, search


@pytest.mark.parametrize("text, expected", [
    ("故障 E17-42 怎么处理", {"E17-42"}),
    ("ｅ１７－４２ 溫度", {"E17-42"}),
    ("seal‑9x 密封圈", {"SEAL-9X"}),
    ("K08 – 03 過熱", {"K08-03"}),
    ("SNS−5B", {"SNS-5B"}),
    ("E17-42、E17-07 和 E17-42", {"E17-42", "E17-07"}),
    ("E17-420 E17-42B XE17-42", {"E17-420", "E17-42B", "XE17-42"}),
    ("ABC-12-X", {"ABC-12-X"}),
    ("E17 设备报代码 42", set()),
    ("SSO sign-in / VPN / 2026-11-01 / 35 摄氏度", set()),
    ("first-time login, no-basis, zh-Hant", set()),
    ("", set()),
])
def test_identifier_boundaries_and_normalization(text, expected):
    assert extract_identifiers(text) == expected


def test_inherited_heading_is_evidence_without_changing_original_glyphs():
    path, text = "感測器 > 故障碼 S5-19 > 排查步驟", "執行零點校正。"
    assert evidence_identifiers(path, text) == {"S5-19"}
    assert text == "執行零點校正。"


def test_code_normalization_preserves_prose_and_traditional_glyphs():
    assert normalize_identifiers("Check ｅ１７－０７ 感測器 sign-in") == "Check E17-07 感測器 sign-in"


@pytest.mark.parametrize("query,expected", [
    ("E17 设备报代码 42 先查什么", {"E17-42"}),
    ("ｅ１７ 設備報代碼：０７ 怎麼處理", {"E17-07"}),
    ("p03设备故障码11", {"P03-11"}),
    ("S5 報錯誤碼 19", {"S5-19"}),
    ("C21故障碼05 校準", {"C21-05"}),
    ("E17代码42 与 P03代码11", {"E17-42", "P03-11"}),
    ("E17代码42、E17-07", {"E17-42", "E17-07"}),
    ("ZE17代码42", {"ZE17-42"}),
])
def test_bounded_split_query_codes(query, expected):
    assert extract_query_identifiers(query) == expected


@pytest.mark.parametrize("query", [
    "E17 温度 42 摄氏度", "E17 42", "E17 环境温度超过35", "E17设备，代码42", "E17\n代码42",
    "E17故障码4", "E17故障码420", "E17代码42B", "E17代码42.0", "E17代码42-43",
    "E17代码42/43", "E17代码42、43", "E17代码42 或 43", "E17代码42至43",
    "E17代码42分钟", "E17代码42元", "E17代码42℃", "E17代码42度", "E17代码42 摄氏度", "E17代码42米",
    "_E17代码42", "E17 日期 2026-11-07",
])
def test_ambiguous_numbers_and_unsupported_split_forms_are_not_inferred(query):
    assert extract_query_identifiers(query) == set()


def test_query_joining_does_not_modify_source_matching_or_unrelated_prose():
    assert normalize_query_identifiers("Check ｅ１７設備報代碼４２，感測器正常") == "Check E17-42,感測器正常"
    assert normalize_query_identifiers("環境溫度 ４２ 度") == "環境溫度 ４２ 度"
    assert evidence_identifiers("E17设备代码42", "检查液位") == set()


def test_match_happens_before_top_k_and_ignores_document_title(enc):
    # A correct inherited section is ranked sixth. An unrelated chunk uses the
    # exact code in its document title only; it must not become valid evidence.
    rows = [SimpleNamespace(id=str(i), dist=0.01 * i, doc_id="manual", title="ZX7-21 manual",
                            version=1, page=i, section_path="Overview", text="General instructions", source_lang="en")
            for i in range(6)]
    rows[-1].section_path = "Fault ZX7-21 > Recovery"
    db = Mock()
    db.execute.return_value.all.return_value = rows
    result = search(db, enc, AccessScope("u", frozenset({"kb"})), "ZX7-21", use_sparse=False, top_k=1)
    assert [e.chunk_id for e in result.evidence] == ["5"]
    assert result.stages["fused"] == [str(i) for i in range(6)]
    assert result.stages["identifier_match"] == ["5"]
    assert result.missing_identifiers == []


def evidence(code, *, dense=0.9):
    return Evidence(code, "doc", "manual", 1, 1, code, "Recovery instructions", "en", 0.02, dense_sim=dense)


def stub_search(monkeypatch, ev, required):
    monkeypatch.setattr(pipeline, "compute_scope", lambda *_: AccessScope("u", frozenset({"kb"})))
    monkeypatch.setattr(pipeline, "search", lambda *a, **kw: SearchResult(ev, {}, required))


def test_partial_multi_code_evidence_is_refused_even_with_high_similarity(monkeypatch):
    stub_search(monkeypatch, [evidence("ZX7-21")], ["ZX7-21", "ZX7-22"])
    out = pipeline.retrieve(None, None, None, None, "比较 ZX7-21 与 ZX7-22")
    assert out.status == pipeline.INSUFFICIENT and not out.evidence
    assert out.reason == "IDENTIFIER_MISMATCH" and out.missing_identifiers == ["ZX7-22"]


def test_multi_code_evidence_can_span_chunks(monkeypatch):
    stub_search(monkeypatch, [evidence("ZX7-21"), evidence("ZX7-22")], ["ZX7-21", "ZX7-22"])
    out = pipeline.retrieve(None, None, None, None, "比较 ZX7-21 与 ZX7-22")
    assert out.status == pipeline.OK and len(out.evidence) == 2


def test_each_requested_code_needs_evidence_above_score_gate(monkeypatch):
    stub_search(monkeypatch, [evidence("ZX7-21"), evidence("ZX7-22", dense=0.01)], ["ZX7-21", "ZX7-22"])
    out = pipeline.retrieve(None, None, None, None, "比较 ZX7-21 与 ZX7-22")
    assert out.status == pipeline.INSUFFICIENT and out.reason == "LOW_SCORE"


def test_rerank_cannot_override_missing_identifiers(monkeypatch):
    stub_search(monkeypatch, [evidence("ZX7-21")], ["ZX7-21", "ZX7-22"])
    rr = Mock()
    rr.score.return_value = [0.99]
    out = pipeline.retrieve(None, None, rr, None, "ZX7-21 ZX7-22")
    assert out.status == pipeline.INSUFFICIENT and out.reason == "IDENTIFIER_MISMATCH"


def test_rerank_top_k_must_still_cover_requested_identifiers(monkeypatch):
    stub_search(monkeypatch, [evidence("ZX7-21"), evidence("ZX7-22")], ["ZX7-21", "ZX7-22"])
    monkeypatch.setattr(pipeline.settings, "final_top_k", 1)
    rr = Mock()
    rr.score.return_value = [0.99, 0.98]
    out = pipeline.retrieve(None, None, rr, None, "ZX7-21 ZX7-22")
    assert out.status == pipeline.INSUFFICIENT and out.missing_identifiers == ["ZX7-22"]


def test_no_access_never_runs_recall_or_rerank(monkeypatch):
    def denied(*_):
        raise NoAccess("permission lookup failed")

    recall, rr = Mock(), Mock()
    monkeypatch.setattr(pipeline, "compute_scope", denied)
    monkeypatch.setattr(pipeline, "search", recall)
    out = pipeline.retrieve(None, None, rr, None, "ZX7-21")
    assert out.status == pipeline.NO_ACCESS and not out.required_identifiers
    recall.assert_not_called()
    rr.score.assert_not_called()
