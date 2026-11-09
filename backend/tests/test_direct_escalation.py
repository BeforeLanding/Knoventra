import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select

from fakes import ScriptedModel, session_factory_for
from knoventra.agent.escalation_intent import direct_escalation_summary
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.db.models import Escalation
import run_eval


@pytest.mark.parametrize("text", [
    "转人工", "帮我转人工", "这个问题我想转人工", "請幫我轉人工", "请转交专家", "我要转给专家",
    "请把这个问题转交给专家处理", "E17-42 复位仍报警，帮我转人工。", "麻煩你幫我轉人工", "能否帮我转人工吗？",
])
def test_clear_requests_use_user_text_as_reviewable_summary(text):
    assert direct_escalation_summary([HumanMessage(text)]) == text


@pytest.mark.parametrize("text", [
    "不要转人工", "我不想转人工", "暂不转人工", "别转人工", "如果还不行，帮我转人工", "若报警，转人工",
    "稍后再转人工", "转人工流程是什么", "怎么转人工", "如何联系人工", "转人工是否收费", "请查询转交进度",
    '“我想转人工”这句话是什么意思', '用户说：我想转人工', '`帮我转人工`', "例如：转人工", "请取消转交",
    "Please transfer me to an expert", "转人工。" + "说明" * 1000,
    "If it fails, 转人工", "除非恢复成功，否则帮我转人工", "请勿转人工", "有人说：帮我转人工",
    "先暂缓，转人工", "How to do it? 转人工",
])
def test_negation_conditions_information_quotes_and_long_input_keep_model_path(text):
    assert direct_escalation_summary([HumanMessage(text)]) is None


def test_resuming_tools_does_not_start_another_direct_operation():
    assert direct_escalation_summary([HumanMessage("转人工"), ToolMessage("已创建记录", tool_call_id="explicit-escalation")]) is None


def test_direct_summary_preserves_prior_user_details_without_treating_model_prose_as_facts():
    prior = "E17-42 复位后仍然报警，已经观察10分钟。"
    messages = [HumanMessage(prior), AIMessage("模型曾说已安排维修，但没有工具证明。"), HumanMessage("帮我转人工")]
    summary = direct_escalation_summary(messages)
    assert prior in summary and "帮我转人工" in summary
    assert "已安排维修" not in summary


def test_context_that_exceeds_summary_limit_keeps_model_path_without_truncating_it():
    assert direct_escalation_summary([HumanMessage("长" * 2000), AIMessage("回复"), HumanMessage("转人工")]) is None


@pytest.mark.parametrize("approve,expected", [(True, 1), (False, 0)])
def test_direct_confirmation_does_not_call_model_before_approval_and_survives_restart(db, enc, users, approve, expected):
    calls = []

    def script(messages):
        calls.append(1)
        assert isinstance(messages[-1], ToolMessage), "the first model call must only describe the stored decision"
        return AIMessage(messages[-1].content)

    saver = InMemorySaver()
    deps = AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver)
    graph = build_graph(deps)
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": users["sg-ops"].id}}
    question = "这个问题我想转人工"
    graph.invoke({"messages": [HumanMessage(question)], "evidence": {}, "sources": [], "error_code": None}, cfg)
    snapshot = graph.get_state(cfg)
    assert snapshot.tasks[0].interrupts[0].value["summary"] == question
    assert calls == [] and db.scalar(select(func.count()).select_from(Escalation)) == 0
    restarted = build_graph(deps)
    out = restarted.invoke(Command(resume={"approve": approve}), cfg)
    assert len(calls) == 1 and not restarted.get_state(cfg).next
    assert db.scalar(select(func.count()).select_from(Escalation)) == expected
    assert "已创建转交记录" in out["messages"][-1].content if approve else "已取消转交" in out["messages"][-1].content


@pytest.mark.parametrize("expected_records", [0, 1])
def test_evaluation_does_not_count_expected_confirmation_as_observed(db, enc, users, expected_records):
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None,
                                  ScriptedModel(script=lambda messages: AIMessage("Please provide more details.")), InMemorySaver()))
    case = {"id": "ignored-workflow", "category": "escalation", "user": "sg-ops",
            "question": "Please transfer me to an expert", "expect": {"decisions": [False], "records": expected_records}}
    row = run_eval.eval_agent(graph, db, users, [case])[0]
    assert row["records"] == 0 and row["asked_confirmation"] is False and row["ok"] is False


def test_evaluation_records_unexpected_confirmation_without_approving_it(db, enc, users):
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None,
                                  ScriptedModel(script=lambda messages: pytest.fail("no model call before approval")), InMemorySaver()))
    case = {"id": "unexpected-card", "category": "escalation", "user": "sg-ops", "question": "转人工",
            "expect": {"decisions": [], "records": 0}}
    row = run_eval.eval_agent(graph, db, users, [case])[0]
    assert row["asked_confirmation"] is True and row["record_count_ok"] is True
    assert row["records"] == 0 and row["ok"] is False
