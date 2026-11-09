import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult
from langgraph.checkpoint.memory import InMemorySaver
from langchain_openai import ChatOpenAI

from answer_timing import AnswerChunkTimer
from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.config import settings
import run_eval


def start(timer, node="agent"):
    run_id = uuid.uuid4()
    timer.on_chat_model_start({}, [], run_id=run_id, metadata={"langgraph_node": node})
    return run_id


def content(timer, run_id, text, **fields):
    timer.on_llm_new_token(text, run_id=run_id,
                           chunk=ChatGenerationChunk(message=AIMessageChunk(content=text, **fields)))


def end(timer, run_id, message):
    timer.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)


def test_first_content_ignores_empty_reasoning_and_other_nodes_then_keeps_first_chunk():
    readings = iter([10.125])
    timer = AnswerChunkTimer(10, clock=lambda: next(readings))
    ignored = start(timer, "tools")
    content(timer, ignored, "not an answer")
    end(timer, ignored, AIMessage("not an answer"))
    run_id = start(timer)
    content(timer, run_id, "", additional_kwargs={"reasoning_content": "private reasoning"})
    content(timer, run_id, "首")
    content(timer, run_id, "个回答分片")
    end(timer, run_id, AIMessage("首个回答分片"))
    assert timer.first_answer_chunk_ms == 125
    assert timer._first == {}


def test_tool_preamble_is_discarded_and_final_answer_is_measured_from_case_start():
    readings = iter([1.1, 1.75])
    timer = AnswerChunkTimer(1, clock=lambda: next(readings))
    first = start(timer)
    content(timer, first, "我先查资料")
    end(timer, first, AIMessage("我先查资料", tool_calls=call("search_knowledge", {"query": "q"}).tool_calls))
    assert timer.first_answer_chunk_ms is None
    final = start(timer)
    content(timer, final, "查询结果")
    end(timer, final, AIMessage("查询结果"))
    assert timer.first_answer_chunk_ms == 750


@pytest.mark.parametrize("message", [
    AIMessage(""),
    AIMessage("半截回答", response_metadata={"finish_reason": "length"}),
    AIMessage("失败", invalid_tool_calls=[{"name": "search_knowledge", "args": "{", "id": "bad", "error": "bad"}]),
])
def test_empty_truncated_or_invalid_response_has_no_answer_sample(message):
    timer = AnswerChunkTimer(0, clock=lambda: 1)
    run_id = start(timer)
    content(timer, run_id, "partial")
    end(timer, run_id, message)
    assert timer.first_answer_chunk_ms is None


def test_non_streaming_completion_and_later_failure_cannot_reuse_a_previous_measurement():
    timer = AnswerChunkTimer(0, clock=lambda: 1)
    first = start(timer)
    content(timer, first, "earlier")
    end(timer, first, AIMessage("earlier"))
    assert timer.first_answer_chunk_ms == 1000
    second = start(timer)
    end(timer, second, AIMessage("non-streamed"))
    assert timer.first_answer_chunk_ms is None
    third = start(timer)
    content(timer, third, "interrupted")
    timer.on_llm_error(RuntimeError("provider failed"), run_id=third)
    assert timer.first_answer_chunk_ms is None and timer._first == {}


class StreamingScriptedModel(ScriptedModel):
    streaming: bool = True

    def _generate(self, messages, **kwargs):
        raise AssertionError("expected the streaming model path")

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.script(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "test"}))
        for part in (response.content[:2], response.content[2:]):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))
        if response.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(response.tool_calls)]))
        yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata=response.response_metadata))


@pytest.mark.parametrize("outcome", ["answer", "refusal", "citation_error", "truncated"])
def test_real_graph_callbacks_reach_evaluator_and_review_snapshot(db, enc, users, monkeypatch, outcome):
    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)
    monkeypatch.setattr(run_eval, "AnswerChunkTimer", lambda started: AnswerChunkTimer(started, clock=lambda: started + 0.125))

    def script(messages):
        if not isinstance(messages[-1], ToolMessage):
            response = call("search_knowledge", {"query": "E17-42 排查"})
            response.content = "先查询资料"
            return response
        text = {"answer": "先检查循环泵 [S1]。", "refusal": "【无依据】没有相关信息。",
                "citation_error": "没有引用的回答", "truncated": "半截回答 [S1]"}[outcome]
        return AIMessage(text, response_metadata={"finish_reason": "length" if outcome == "truncated" else "stop"})

    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, StreamingScriptedModel(script=script, streaming=True), InMemorySaver()))
    results = run_eval.eval_agent(graph, db, users, [run_eval.load_cases("dev")[0]])
    row = results[0]
    expected = 125 if outcome in ("answer", "refusal") else None
    assert row["first_answer_chunk_ms"] == expected
    assert row["review_snapshot"]["answer"] == row["answer"]
    assert row["diagnostics"]["retrieval_trace"]
    assert run_eval.summarize_agent(results)["first_answer_chunk_ms"]["n"] == int(expected is not None)


def test_confirmation_resume_keeps_same_case_timer_and_does_not_duplicate_write(db, enc, users, monkeypatch):
    monkeypatch.setattr(run_eval, "AnswerChunkTimer", lambda started: AnswerChunkTimer(started, clock=lambda: started + 0.5))

    def script(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("已处理转交决定。")
        response = call("escalate_to_expert", {"summary": "测试问题"})
        response.content = "我会转交"
        return response

    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, StreamingScriptedModel(script=script, streaming=True), InMemorySaver()))
    case = next(c for c in run_eval.load_cases("dev") if c["category"] == "escalation")
    row = run_eval.eval_agent(graph, db, users, [case])[0]
    assert row["ok"] and row["first_answer_chunk_ms"] == 500


def test_summary_includes_only_measured_answer_chunks():
    rows = [{"category": "legacy", "first_answer_chunk_ms": value} for value in (None, 10, 20)]
    assert run_eval.summarize_agent(rows)["first_answer_chunk_ms"] == {"n": 2, "p50": 10, "p95": 20, "max": 20}


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_openai_adapter_observes_mock_http_stream_without_network(monkeypatch, finish_reason):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        deltas = [{"role": "assistant", "content": ""}, {"reasoning_content": "excluded"},
                  {"content": "你"}, {"content": "好"}, {}]
        chunks = [{"id": "chat-offline", "object": "chat.completion.chunk", "created": 1, "model": "offline-model",
                   "choices": [{"index": 0, "delta": delta,
                                "finish_reason": finish_reason if i == len(deltas) - 1 else None}]}
                  for i, delta in enumerate(deltas)]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    monkeypatch.setattr(run_eval, "AnswerChunkTimer", lambda started: AnswerChunkTimer(started, clock=lambda: started + 0.25))
    forbidden_session = Mock(side_effect=AssertionError("greeting must not access database"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(base_url="https://model.invalid/v1", api_key="offline-token", model="offline-model",
                         streaming=True, max_tokens=100, max_retries=0, http_client=client)
        graph = build_graph(AgentDeps(forbidden_session, None, None, llm, InMemorySaver()))
        case = {"id": "offline-stream", "category": "no_basis", "user": "tester", "question": "你好", "expect": {}}
        row = run_eval.eval_agent(graph, None, {"tester": SimpleNamespace(id="user")}, [case])[0]
    assert len(requests) == 1 and requests[0]["stream"] is True
    assert requests[0].get("max_tokens", requests[0].get("max_completion_tokens")) == 100
    assert row["first_answer_chunk_ms"] == (250 if finish_reason == "stop" else None)
    assert row["error_code"] == (None if finish_reason == "stop" else "MODEL_OUTPUT_TRUNCATED")
    forbidden_session.assert_not_called()
