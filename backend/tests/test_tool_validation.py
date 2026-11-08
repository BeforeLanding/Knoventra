import uuid
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakes import ScriptedModel
from knoventra.agent.graph import AgentDeps, _valid_tool_batch, build_graph, TOOLS


def tool(name="search_knowledge", args=None, cid="one"):
    return {"name": name, "args": {"query": "E17-42"} if args is None else args, "id": cid, "type": "tool_call"}


@pytest.mark.parametrize("calls", [
    [tool(args={})], [tool(args={"query": 42})], [tool(args={"query": None})],
    [tool(args={"query": " \n\t"})], [tool(args={"query": "q" * 4001})],
    [tool(args={"query": "E17", "user_id": "other"})],
    [tool("get_escalation_status", {"user_id": "other"})],
    [tool("escalate_to_expert", {})], [tool("escalate_to_expert", {"summary": False})],
    [tool("escalate_to_expert", {"summary": "　 "})],
    [tool("escalate_to_expert", {"summary": "s" * 2001})],
    [tool("escalate_to_expert", {"summary": "E17", "approve": True})],
    [tool("unknown_tool", {})], [tool(cid=None)], [tool(cid=" ")],
    [tool(), tool(cid="one")],
    [tool("escalate_to_expert", {"summary": "a"}), tool("escalate_to_expert", {"summary": "b"}, "two")],
])
def test_invalid_batch_stops_before_any_session_or_sibling_tool(calls):
    # Put a valid write first to prove prevalidation covers the entire response.
    mixed = [tool("escalate_to_expert", {"summary": "valid sibling"}, "valid-first"), *calls]
    assert not _valid_tool_batch(mixed)
    factory = Mock(side_effect=AssertionError("must not execute any tool"))
    response = AIMessage("", tool_calls=mixed)
    llm = Mock()
    llm.bind_tools.return_value.invoke.return_value = response
    graph = build_graph(AgentDeps(factory, None, None, llm, InMemorySaver()))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": "server-user"}}
    result = graph.invoke({"messages": [HumanMessage("请求")], "evidence": {}, "sources": [], "error_code": None}, cfg)
    assert result["error_code"] == "MODEL_TOOL_CALL_INVALID" and not graph.get_state(cfg).next
    assert result["pending"] is None and result["sources"] == []
    assert not result["messages"][-1].tool_calls
    factory.assert_not_called()
    llm.bind_tools.return_value.invoke.assert_called_once()


def test_valid_tool_contracts_and_boundaries_match_advertised_schemas():
    calls = [tool(args={"query": "q" * 4000}), tool("get_escalation_status", {}, "two"),
             tool("escalate_to_expert", {"summary": "s" * 2000}, "three")]
    assert _valid_tool_batch(calls)
    schemas = {t.name: t.args_schema.model_json_schema() for t in TOOLS}
    assert all(s["additionalProperties"] is False for s in schemas.values())
    assert schemas["search_knowledge"]["properties"]["query"]["maxLength"] == 4000
    assert schemas["escalate_to_expert"]["properties"]["summary"]["maxLength"] == 2000


def test_legacy_tools_checkpoint_is_validated_before_execution():
    factory = Mock(side_effect=AssertionError("legacy invalid call must not execute"))
    llm = ScriptedModel(script=lambda msgs: AIMessage("must not invoke"))
    graph = build_graph(AgentDeps(factory, None, None, llm, InMemorySaver()))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": "server-user"}}
    graph.update_state(cfg, {"messages": [HumanMessage("旧请求"), AIMessage("", tool_calls=[tool(args={})])],
                             "evidence": {}, "sources": [], "error_code": None}, as_node="agent")
    result = graph.invoke(None, cfg)
    assert result["error_code"] == "MODEL_TOOL_CALL_INVALID" and not graph.get_state(cfg).next
    assert not result["messages"][-1].tool_calls
    factory.assert_not_called()
