from unittest.mock import Mock

import httpx
import json
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from fakes import ScriptedModel
from knoventra.agent.model import chat_model_kwargs
from knoventra.config import ModelConfigurationError, Settings, settings


@pytest.mark.parametrize("value", ["enabled", "off", "DISABLED", "", None])
def test_unknown_thinking_config_fails_explicitly(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_thinking=value)


def test_runtime_unknown_thinking_config_cannot_silently_fall_back(monkeypatch):
    monkeypatch.setattr(settings, "llm_thinking", "typo")
    with pytest.raises(ModelConfigurationError, match="llm_thinking"):
        chat_model_kwargs()


@pytest.mark.parametrize("mode", ["provider-default", "disabled"])
def test_web_and_eval_factories_share_thinking_and_keep_their_execution_limits(monkeypatch, mode):
    import langchain_openai
    import run_eval
    from knoventra.api import chat
    from knoventra.agent import checkpoint

    monkeypatch.setattr(settings, "llm_model", "offline-model")
    monkeypatch.setattr(settings, "llm_thinking", mode)
    constructor = Mock(return_value=ScriptedModel(script=lambda msgs: AIMessage("offline")))
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", constructor)
    monkeypatch.setattr(chat, "make_checkpointer", lambda url: (InMemorySaver(), Mock()))
    monkeypatch.setattr(checkpoint, "make_checkpointer", lambda url: (InMemorySaver(), Mock()))
    monkeypatch.setattr(chat, "get_encoder", lambda: None)
    monkeypatch.setattr(chat, "get_reranker", lambda: None)
    chat.get_graph.cache_clear()
    try:
        chat.get_graph()
        _, pool = run_eval.make_evaluation_graph(None, None, 700)
        web, evaluation = [c.kwargs for c in constructor.call_args_list]
        for options in (web, evaluation):
            assert options["streaming"] is True
            if mode == "disabled":
                assert options["extra_body"] == {"thinking": {"type": "disabled"}}
            else:
                assert "extra_body" not in options
        assert web["max_retries"] == 1 and "max_tokens" not in web
        assert evaluation["max_retries"] == 0 and evaluation["max_tokens"] == 700
        pool.close()
    finally:
        chat.get_graph.cache_clear()


@pytest.mark.parametrize("mode", ["provider-default", "disabled"])
def test_real_adapter_serializes_opt_in_switch_without_network(monkeypatch, mode):
    monkeypatch.setattr(settings, "llm_model", "offline-model")
    monkeypatch.setattr(settings, "llm_base_url", "https://model.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "offline-key")
    monkeypatch.setattr(settings, "llm_thinking", mode)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                                       "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": "ok"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(**chat_model_kwargs(), http_client=client, max_retries=0)
        assert llm.invoke([HumanMessage("test")]).content == "ok"
    assert len(requests) == 1
    if mode == "disabled":
        assert requests[0]["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in requests[0]


def test_preflight_records_thinking_without_calling_provider(tmp_path, monkeypatch):
    import run_eval
    import sys

    monkeypatch.setattr(settings, "llm_thinking", "disabled")
    path = tmp_path / "plan.json"
    monkeypatch.setattr(sys, "argv", ["eval", "--mode", "agent", "--dry-run", "--case-id", "ops-01", "--out", str(path)])
    run_eval.main()
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["llm_thinking"] == "disabled" and plan["llm_streaming"] is True
