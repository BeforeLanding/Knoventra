from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from knoventra.access.scope import AccessScope
from knoventra.config import settings
from knoventra.retrieval import pipeline
from knoventra.retrieval import rerank as module
from knoventra.retrieval.search import Evidence, SearchResult


def evidence(cid, section="", text="操作步骤", dense=0.95, sparse=0.9):
    return Evidence(cid, "doc", "手册", 1, 1, section, text, "zh-Hans", 0.02,
                    dense_sim=dense, sparse_ip=sparse)


def test_rerank_preserves_heading_context_and_original_source():
    ev = [evidence("a", "故障碼 S5-19 > 排查步驟", "執行零點校正。"), evidence("b")]
    rr = Mock()
    rr.score.return_value = [0.2, 0.9]
    ranked = module.rerank(rr, "s5‑19 零點校正", ev, top_k=1)
    rr.score.assert_called_once_with("S5-19 零點校正", ["故障碼 S5-19 > 排查步驟\n執行零點校正。", "操作步骤"])
    assert ranked[0].chunk_id == "b" and ranked[0].rerank_score == 0.9
    assert all(e.rerank_score is None for e in ev)  # no partial mutations to shared recall results
    assert ev[0].text == "執行零點校正。"


def test_rerank_uses_same_split_code_normalization_as_recall():
    rr = Mock()
    rr.score.return_value = [0.9]
    original = evidence("a", "故障碼 S5-19", "執行零點校正。")
    module.rerank(rr, "s5設備報錯誤碼19 如何處理", [original], top_k=1)
    rr.score.assert_called_once_with("S5-19 如何處理", ["故障碼 S5-19\n執行零點校正。"])
    assert original.text == "執行零點校正。"


@pytest.mark.parametrize("scores", [[], [0.9], [0.1, 0.2, 0.3], [float("nan"), 0.8],
                                    [float("inf"), 0.8], [-0.1, 0.8], [1.1, 0.8], [True, 0.8],
                                    [None, 0.8], ["0.8", 0.8]])
def test_bad_reranker_output_fails_closed_without_partial_mutation(scores):
    rr = Mock()
    rr.score.return_value = scores
    ev = [evidence("a"), evidence("b")]
    with pytest.raises(ValueError):
        module.rerank(rr, "q", ev, 2)
    assert all(e.rerank_score is None for e in ev)


def test_empty_candidates_skip_model_and_ties_keep_recall_order():
    rr = Mock()
    assert module.rerank(rr, "q", [], 5) == []
    rr.score.assert_not_called()
    rr.score.return_value = [0.5, 0.5]
    assert [e.chunk_id for e in module.rerank(rr, "q", [evidence("a"), evidence("b")], 2)] == ["a", "b"]


@pytest.mark.parametrize("raw", [0.8, np.float32(0.8), [0.8], np.array([0.8])])
def test_flagembedding_single_result_shapes_are_supported(raw):
    rr = module.BgeReranker.__new__(module.BgeReranker)
    rr.m = Mock()
    rr.m.compute_score.return_value = raw
    assert rr.score("q", ["text"]) == pytest.approx([0.8])
    rr.m.compute_score.assert_called_once_with([["q", "text"]], normalize=True)


def test_disabled_rerank_does_not_load_model_and_model_selection_is_explicit(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(module, "_load_reranker", loader)
    monkeypatch.setattr(settings, "rerank", "off")
    assert module.get_reranker() is None
    loader.assert_not_called()
    monkeypatch.setattr(settings, "rerank", "bge")
    monkeypatch.setattr(settings, "rerank_model", "example/local-model")
    assert module.get_reranker() is loader.return_value
    loader.assert_called_once_with("example/local-model")


def stub_recall(monkeypatch, ev):
    monkeypatch.setattr(pipeline, "compute_scope", lambda *_: AccessScope("u", frozenset({"kb"})))
    recall = Mock(side_effect=lambda *a, **kw: SearchResult(ev[:kw["top_k"]], {}))
    monkeypatch.setattr(pipeline, "search", recall)
    return recall


def test_separate_rerank_threshold_never_falls_back_to_high_dense_or_sparse_score(monkeypatch):
    stub_recall(monkeypatch, [evidence("a")])
    monkeypatch.setattr(settings, "gate_threshold", 0.01)
    monkeypatch.setattr(settings, "rerank_threshold", 0.8)
    rr = Mock()
    rr.score.return_value = [0.7]
    out = pipeline.retrieve(None, None, rr, None, "q")
    assert out.status == pipeline.INSUFFICIENT and out.reason == "LOW_SCORE" and out.evidence == []
    assert out.ranked_evidence[0].rerank_score == 0.7
    assert pipeline.retrieve(None, None, rr, None, "q", threshold=0.6).status == pipeline.OK
    assert pipeline.retrieve(None, None, None, None, "q").status == pipeline.OK


def test_eval_measures_reranked_top_k_and_keeps_diagnostics_on_refusal(monkeypatch):
    import run_eval

    ev = [evidence(str(i)) for i in range(6)]
    ev[-1].document_title = "目标手册"
    recall = stub_recall(monkeypatch, ev)
    monkeypatch.setattr(settings, "final_top_k", 5)
    monkeypatch.setattr(settings, "rerank_candidates", 10)
    cases = [{"id": "rank-regression", "category": "ops", "user": "u", "question": "如何处理", "expect":
              {"title": "目标手册", "pages": [1], "revision": 1}}]
    options = dict(use_sparse=True, use_opencc=True, threshold=0.99)
    base = run_eval.eval_retrieval(None, None, None, {"u": SimpleNamespace(id="u")}, cases, **options)[0]
    assert base["hit"] is False and recall.call_count == 1
    recall.reset_mock()
    rr = Mock()
    rr.score.return_value = [0.1] * 5 + [0.9]
    actual = run_eval.eval_retrieval(None, None, rr, {"u": SimpleNamespace(id="u")}, cases, **options)[0]
    assert actual["hit"] is True and actual["rank"] == 1 and actual["gated_out"] is True
    assert actual["ranked_evidence"][0]["chunk_id"] == "5"
    assert actual["ranked_evidence"][0]["rerank_score"] == 0.9 and recall.call_count == 1
