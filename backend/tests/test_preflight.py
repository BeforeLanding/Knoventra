import json
import sys
from unittest.mock import Mock

import pytest

import preflight
import run_eval


CASES = [{"id": "a", "category": "ops"}, {"id": "b", "category": "cross"}]


def plan(**kwargs):
    options = dict(mode="agent", split="dev", tool_limit=4, model="example-model",
                   max_output_tokens=1000, assumed_input_tokens=4000,
                   input_price=1.0, output_price=2.0, currency="CNY")
    return preflight.plan(CASES, **{**options, **kwargs})


def test_case_selection_preserves_order_deduplicates_and_rejects_unknown():
    assert preflight.select_cases(CASES, ["b", "a", "b"]) == CASES
    with pytest.raises(ValueError, match="absent"):
        preflight.select_cases(CASES, ["missing"])


def test_plan_reports_scenario_assumptions_and_call_bound():
    result = plan()
    assert result["graph_model_call_bound"] == 10
    assert result["requested_output_token_bound"] == 10000
    assert result["scenario_cost_estimate"] == pytest.approx(0.06)
    assert result["currency"] == "CNY" and result["automatic_provider_retries"] == 0
    assert "not a billing cap" in result["limitation"]
    assert plan(mode="retrieval")["graph_model_call_bound"] == 0
    assert plan(input_price=None, output_price=None)["scenario_cost_estimate"] is None
    escalation = [{"id": "confirm-case", "category": "escalation"}]
    options = dict(split="dev", tool_limit=4, model="", max_output_tokens=1024,
                   assumed_input_tokens=4000, input_price=None, output_price=None, currency="USD")
    assert preflight.plan(escalation, mode="agent", **options)["confirmation_decision_cases"] == ["confirm-case"]
    assert preflight.plan(escalation, mode="retrieval", **options)["confirmation_decision_cases"] == []


@pytest.mark.parametrize("kwargs", [
    {"max_output_tokens": 0}, {"assumed_input_tokens": -1}, {"tool_limit": -1},
    {"input_price": -1}, {"input_price": float("nan")}, {"output_price": float("inf")},
    {"input_price": None}, {"currency": " "}, {"input_price": 1e308},
])
def test_invalid_budget_inputs_fail_explicitly(kwargs):
    with pytest.raises(ValueError):
        plan(**kwargs)


def test_preflight_does_not_connect_db_or_load_encoder(tmp_path, monkeypatch, capsys):
    from knoventra.db import session
    from knoventra.ingestion import encoder
    from knoventra.config import settings
    forbidden = Mock(side_effect=AssertionError("preflight must remain offline"))
    monkeypatch.setattr(session, "SessionLocal", forbidden)
    monkeypatch.setattr(encoder, "get_encoder", forbidden)
    monkeypatch.setattr(settings, "llm_api_key", "must-not-leak-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://private.example.test/v1")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://private-secret@db/app")
    selected = run_eval.load_cases("dev")[0]["id"]
    path = tmp_path / "preflight.json"
    monkeypatch.setattr(sys, "argv", ["eval", "--mode", "agent", "--dry-run", "--case-id", selected,
                                     "--case-id", selected, "--out", str(path)])
    run_eval.main()
    content = path.read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["mode"] == "preflight" and data["case_ids"] == [selected] and data["n_cases"] == 1
    assert all(secret not in content for secret in ("must-not-leak-key", "private.example.test", "private-secret"))
    assert "must-not-leak-key" not in capsys.readouterr().out
    forbidden.assert_not_called()


@pytest.mark.parametrize("flags", [[], ["--allow-llm"]])
def test_agent_requires_explicit_opt_in_and_supplied_prices_before_loading_models(monkeypatch, flags):
    from knoventra.ingestion import encoder
    loader = Mock(side_effect=AssertionError("must not load"))
    monkeypatch.setattr(encoder, "get_encoder", loader)
    monkeypatch.setattr(sys, "argv", ["eval", "--mode", "agent", *flags])
    with pytest.raises(SystemExit) as error:
        run_eval.main()
    assert error.value.code == 2
    loader.assert_not_called()


def test_eval_graph_sets_output_cap_and_disables_automatic_provider_retries(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from fakes import ScriptedModel
    from knoventra.config import settings
    from knoventra.agent import checkpoint
    import langchain_openai
    model = ScriptedModel(script=lambda msgs: AIMessage("offline"))
    constructor = Mock(return_value=model)
    pool = Mock()
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", constructor)
    monkeypatch.setattr(checkpoint, "make_checkpointer", lambda url: (InMemorySaver(), pool))
    monkeypatch.setattr(settings, "llm_model", "configured-test-model")
    graph, returned_pool = run_eval.make_evaluation_graph(None, None, 700)
    assert graph is not None and returned_pool is pool
    assert constructor.call_args.kwargs["max_tokens"] == 700
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["streaming"] is False
    pool.close()
