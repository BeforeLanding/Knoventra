"""D11 working memory: Redis store, slot drafts rebuilt from the checkpoint, context compression."""
import os
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import select

from fakes import DictWorkingMemory, ScriptedModel, call, record_decision, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.config import Settings, settings
from knoventra.db.models import HelpRequest
from knoventra.memory.context import SUMMARY_HEADER, dialogue, plan_window
from knoventra.memory.drafts import checkpoint_draft
from knoventra.memory.working import RedisWorkingMemory, WorkingMemoryUnavailable

REDIS_URL = os.environ.get("KNV_TEST_REDIS_URL", "redis://localhost:56379/15")


@pytest.fixture(scope="session")
def redis_client():
    import redis

    client = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
    try:
        client.ping()
    except redis.RedisError as exc:
        if os.environ.get("KNV_REQUIRE_REDIS"):
            pytest.fail(f"Redis required but not reachable at {REDIS_URL}: {exc}")
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    return client


@pytest.fixture
def wm(redis_client):
    memory = RedisWorkingMemory(redis_client, ttl_s=60)
    yield memory
    for key in redis_client.scan_iter("knv:wm:test-*"):
        redis_client.delete(key)


# ------------------------------------------------------------------ Redis store

def test_redis_store_round_trip_ttl_and_delete(wm, redis_client):
    user, thread = f"test-{uuid.uuid4().hex[:8]}", "t1"
    key = wm.key(user, thread)
    assert key == f"knv:wm:{user}:t1" and wm.load(user, thread) == {}
    wm.save(user, thread, {"draft": {"anchor": "a", "value": {"machine_id": "610101"}}, "summary": {"text": "摘要"}})
    assert 0 < redis_client.ttl(key) <= 60
    redis_client.expire(key, 5)
    assert wm.load(user, thread)["draft"]["value"] == {"machine_id": "610101"}
    assert redis_client.ttl(key) > 5  # reading renews the TTL
    wm.save(user, thread, {"draft": None})
    assert wm.load(user, thread) == {"summary": {"text": "摘要"}}


def test_redis_corrupt_field_is_a_miss(wm, redis_client):
    user = f"test-{uuid.uuid4().hex[:8]}"
    redis_client.hset(wm.key(user, "t"), mapping={"draft": b"\xff{not json", "summary": '{"text": "ok"}'})
    assert wm.load(user, "t") == {"summary": {"text": "ok"}}


def test_unreachable_redis_raises_unavailable():
    import redis

    dead = RedisWorkingMemory(redis.Redis(port=1, socket_connect_timeout=0.2, socket_timeout=0.2), ttl_s=60)
    with pytest.raises(WorkingMemoryUnavailable):
        dead.load("u", "t")
    with pytest.raises(WorkingMemoryUnavailable):
        dead.save("u", "t", {"draft": None})


def test_keep_turns_must_cover_an_open_draft():
    with pytest.raises(ValidationError):
        Settings(context_keep_turns=1)


# ------------------------------------------------------------------ slot drafts

def slot_agent(messages):
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage("请问你已经做过哪些处理？" if "还缺" in last.content else "已处理。")
    said = last.content
    if "610101" in said:
        return call("request_help", {"machine_id": "610101", "symptom": "切换 tank 时报 A-1301，pump 不转"}, "h1")
    if "620202" in said:
        return call("request_help", {"machine_id": "620202", "symptom": "door 无法关闭"}, "h3")
    return call("request_help", {"tried_steps": said}, "h2")


class Turns:
    def __init__(self, db, enc, user, memory, agent=slot_agent, summarizer=None):
        self.graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent),
                                           InMemorySaver(), memory=memory, summarizer=summarizer))
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
        self.key = f"{user.id}:{self.cfg['configurable']['thread_id']}"
        self.db = db

    def say(self, text):
        if isinstance(text, dict):
            record_decision(self.db, self.graph, self.cfg, text["approve"])
        inp = Command(resume=text) if isinstance(text, dict) else {
            "messages": [HumanMessage(text, id=uuid.uuid4().hex)], "evidence": {}, "sources": [], "error_code": None}
        return self.graph.invoke(inp, self.cfg)


def draft_events(out):
    return [e for e in out.get("memory_trace") or [] if e["event"] == "draft"]


def field_requests(db, user):
    return db.scalars(select(HelpRequest).where(HelpRequest.user_id == user.id, HelpRequest.kind == "field_help")).all()


def test_draft_working_copy_is_anchored_to_the_checkpoint(db, enc, users):
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory)
    out = t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    tool = next(m for m in out["messages"] if isinstance(m, ToolMessage))
    cached = memory.data[t.key]["draft"]
    assert cached["anchor"] == tool.id and cached["value"] == tool.artifact["help_draft"]
    assert cached["value"]["machine_id"] == "610101"
    assert "help_draft" not in out  # not graph state any more

    out = t.say("已经重新插紧 LS-2")
    assert draft_events(out)[0]["status"] == "hit"
    assert memory.data[t.key]["draft"]["anchor"] == f"card:{out['pending']['operation_id']}"
    t.say({"approve": True})
    assert "draft" not in memory.data[t.key]
    assert field_requests(db, users["sg-ops"])[0].tried_steps == "已经重新插紧 LS-2"


def test_lost_redis_draft_is_rebuilt_from_the_checkpoint(db, enc, users):
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory)
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    memory.data.clear()  # Redis restarted / key evicted between turns
    out = t.say("已经重新插紧 LS-2")
    assert draft_events(out)[0]["status"] == "rebuilt"
    card = out["__interrupt__"][0].value["summary"]
    for label in ("机台号：610101", "机型：WX-38", "告警码：A-1301", "已尝试的步骤：已经重新插紧 LS-2"):
        assert label in card
    assert memory.data[t.key]["draft"]["value"]["machine_id"] == "610101"  # repaired


def test_pending_card_is_the_durable_draft_while_confirming(db, enc, users):
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory)
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    out = t.say("已经重新插紧 LS-2")
    anchor, slots = checkpoint_draft(out["messages"], out["pending"])
    assert anchor == f"card:{out['pending']['operation_id']}" and slots["tried_steps"] == "已经重新插紧 LS-2"
    memory.data.clear()
    t.say({"approve": True})
    req = field_requests(db, users["sg-ops"])[0]
    assert (req.machine_id, req.alarm_codes, req.tried_steps) == ("610101", ["A-1301"], "已经重新插紧 LS-2")


def test_redis_outage_degrades_without_losing_slots(db, enc, users):
    memory = DictWorkingMemory()
    memory.down = True
    t = Turns(db, enc, users["sg-ops"], memory)
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    out = t.say("已经重新插紧 LS-2")
    assert draft_events(out)[0] == {"event": "draft", "status": "rebuilt", "wm": "unavailable"}
    t.say({"approve": True})
    assert field_requests(db, users["sg-ops"])[0].machine_id == "610101"


def test_stale_redis_draft_never_leaks_into_a_new_request(db, enc, users):
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory)
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    t.say("已经重新插紧 LS-2")
    stale = memory.data[t.key]["draft"]
    memory.down = True  # the clear after filing fails
    t.say({"approve": True})
    memory.down = False
    memory.data[t.key]["draft"] = stale
    out = t.say("另一台 620202 door 无法关闭，也帮我提单")
    tool = [m for m in out["messages"] if isinstance(m, ToolMessage)][-1]
    assert tool.artifact["help_draft"]["machine_id"] == "620202"
    assert "tried_steps" not in tool.artifact["help_draft"] and "已尝试" in tool.content
    assert memory.data[t.key]["draft"]["anchor"] == tool.id


def test_cancel_clears_working_copy_and_marks_the_checkpoint(db, enc, users):
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory)
    t.say("机台 610101 pump 不转")
    out = t.say("算了")
    assert out["messages"][-1].response_metadata["help_draft_cleared"] is True
    assert checkpoint_draft(out["messages"], out.get("pending")) == (None, None)
    assert "draft" not in memory.data[t.key]


def test_draft_rebuilds_against_real_redis(db, enc, users, wm, redis_client):
    t = Turns(db, enc, users["sg-ops"], wm)
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    key = wm.key(users["sg-ops"].id, t.cfg["configurable"]["thread_id"])
    assert redis_client.hexists(key, "draft")
    redis_client.delete(key)
    out = t.say("已经重新插紧 LS-2")
    assert draft_events(out)[0] == {"event": "draft", "status": "rebuilt", "wm": "ok"}
    assert "机台号：610101" in out["__interrupt__"][0].value["summary"]
    redis_client.delete(key)


# ------------------------------------------------------------------ context compression

LONG = "这是一段较长的回答内容，用来让历史超过预算。" * 6


def chat_turns(n):
    out = []
    for i in range(n):
        out += [HumanMessage(f"第{i}个问题 机台 61010{i}", id=f"h{i}"),
                AIMessage("", tool_calls=[{"name": "search_knowledge", "args": {"query": "q"}, "id": f"c{i}",
                                           "type": "tool_call"}], id=f"a{i}"),
                ToolMessage(f"[S1] 机密证据正文 {i}", tool_call_id=f"c{i}", id=f"t{i}"),
                AIMessage(f"回答{i} [S1] {LONG}", id=f"r{i}")]
    return out


class Recorder:
    def __init__(self, text="摘要：用户问过机台 610100 的问题 [S1]"):
        self.requests, self.text = [], text

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def test_within_budget_sends_everything():
    msgs = chat_turns(2)
    win = plan_window(msgs, None, budget=100_000, keep_turns=3, summarize=Recorder())
    assert (win.summary, win.from_id, win.trace["status"]) == (None, None, "within_budget")


def test_over_budget_summarizes_older_turns_on_turn_boundaries():
    msgs, rec = chat_turns(6), Recorder()
    win = plan_window(msgs, None, budget=300, keep_turns=3, summarize=rec)
    assert win.trace["status"] == "generated" and win.from_id == "h3"
    assert win.new_record == {"text": rec.text, "upto_id": "r2"}
    assert win.trace["tokens_after"] < win.trace["tokens_before"]
    body = rec.requests[0][1].content
    assert "第0个问题" in body and "第3个问题" not in body
    assert "机密证据正文" not in body  # tool output / evidence never reaches a summary


def test_summary_is_reused_then_rolled_forward():
    msgs, rec = chat_turns(6), Recorder()
    first = plan_window(msgs, None, budget=300, keep_turns=3, summarize=rec)
    again = plan_window(msgs, first.new_record, budget=300, keep_turns=3, summarize=rec)
    assert again.trace["status"] == "reused" and len(rec.requests) == 1

    longer = chat_turns(9)
    rolled = plan_window(longer, first.new_record, budget=300, keep_turns=3, summarize=rec)
    assert rolled.trace["status"] == "rolled" and rolled.from_id == "h6"
    body = rec.requests[1][1].content
    assert body.startswith("已有摘要：\n" + rec.text) and "第3个问题" in body and "第0个问题" not in body


def test_unknown_summary_record_is_regenerated():
    msgs, rec = chat_turns(6), Recorder()
    win = plan_window(msgs, {"text": "别的会话的摘要", "upto_id": "elsewhere"}, budget=300, keep_turns=3, summarize=rec)
    assert win.trace["status"] == "generated" and "别的会话的摘要" not in rec.requests[0][1].content


def test_summarizer_failure_keeps_the_full_history():
    win = plan_window(chat_turns(6), None, budget=300, keep_turns=3, summarize=Recorder(TimeoutError()))
    assert (win.summary, win.from_id, win.trace["status"], win.trace["error"]) == (None, None, "failed", "TimeoutError")


def test_dialogue_skips_tool_traffic():
    text = dialogue(chat_turns(1))
    assert text.startswith("用户：第0个问题") and "助手：回答0" in text and "机密证据" not in text


def test_graph_compresses_the_model_view_but_not_the_checkpoint(db, enc, users, monkeypatch):
    monkeypatch.setattr(settings, "context_budget_tokens", 120)  # one turn here is ~46 approx tokens
    monkeypatch.setattr(settings, "context_keep_turns", 2)
    seen = []

    def agent(messages):
        seen.append(messages)
        return AIMessage(f"闲聊回答。{LONG}")
    summaries = []

    def summarizer(messages):
        summaries.append(messages)
        return AIMessage("较早时用户问过机台 610101。[S2]")
    memory = DictWorkingMemory()
    t = Turns(db, enc, users["sg-ops"], memory, agent=agent, summarizer=ScriptedModel(script=summarizer))
    for i in range(5):
        out = t.say(f"第{i}轮：你好")
    system = seen[-1][0]
    assert isinstance(system, SystemMessage) and SUMMARY_HEADER in system.content
    assert system.content.endswith("较早时用户问过机台 610101。")  # [S2] marker stripped
    humans = [m.content for m in seen[-1] if isinstance(m, HumanMessage)]
    assert humans[-2:] == ["第3轮：你好", "第4轮：你好"] and "第0轮：你好" not in humans
    checkpoint = out["messages"]
    assert len([m for m in checkpoint if isinstance(m, HumanMessage)]) == 5  # checkpoint keeps all
    ctx = next(e for e in out["memory_trace"] if e["event"] == "context")
    assert ctx["tokens_after"] <= ctx["budget"] < ctx["tokens_before"] and ctx["wm"] == "ok"
    start = next(i for i, m in enumerate(checkpoint) if m.id == out["context_window"]["from_id"])
    assert seen[-1][1].id == checkpoint[start].id  # the model view starts exactly at the window
    assert memory.data[t.key]["summary"]["upto_id"] == checkpoint[start - 1].id
    calls = len(summaries)

    memory.data.clear()  # working memory lost: the summary is regenerated from the checkpoint
    out = t.say("第5轮：你好")
    ctx = next(e for e in out["memory_trace"] if e["event"] == "context")
    assert ctx["status"] == "generated" and len(summaries) == calls + 1
    assert "summary" in memory.data[t.key]


def test_window_survives_the_confirmation_interrupt(db, enc, users, monkeypatch):
    monkeypatch.setattr(settings, "context_budget_tokens", 120)
    monkeypatch.setattr(settings, "context_keep_turns", 2)
    seen = []

    def agent(messages):
        seen.append(messages)
        last = messages[-1]
        if isinstance(last, HumanMessage) and "第" in last.content:
            return AIMessage(f"闲聊回答。{LONG}")
        return slot_agent(messages)
    t = Turns(db, enc, users["sg-ops"], DictWorkingMemory(), agent=agent,
              summarizer=ScriptedModel(script=lambda m: AIMessage("早先闲聊。")))
    for i in range(4):
        t.say(f"第{i}轮：你好")
    t.say("WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提单")
    window = t.say("已经重新插紧 LS-2")["context_window"]
    out = t.say({"approve": True})  # resumes mid-turn: compact does not re-run
    assert out["context_window"] == window and window["summary"] == "早先闲聊。"
    ids = [m.id for m in out["messages"]]
    assert SUMMARY_HEADER in seen[-1][0].content
    # The view starts at the window, or later if the assembler trimmed earlier turns to fit.
    assert ids.index(seen[-1][1].id) >= ids.index(window["from_id"])
    assert field_requests(db, users["sg-ops"])[0].machine_id == "610101"
