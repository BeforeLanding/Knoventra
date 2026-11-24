"""Local UI check ONLY: runs the real app with a scripted fake LLM (no API key needed).

    KNV_ENCODER=hash KNV_GATE_THRESHOLD=0.6 uv run python dev/mock_server.py

Answers are canned, so this demonstrates UI/flow behaviour (sources, refusal, confirm, help
requests, skill lookup, memory recall), not answer quality. The script only decides which
tool to call from fixed keywords; everything after that (permissions, retrieval, slots,
confirmation, recommendation, memory re-validation) is the real server code.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import uvicorn  # noqa: E402
from fakes import ScriptedModel, call  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from knoventra.agent.checkpoint import make_checkpointer  # noqa: E402
from knoventra.agent.graph import AgentDeps, build_graph  # noqa: E402
from knoventra.api.chat import _session, get_graph  # noqa: E402
from knoventra.config import settings  # noqa: E402
from knoventra.ingestion.encoder import get_encoder  # noqa: E402
from knoventra.main import app  # noqa: E402
from knoventra.memory.assembler import DRAFT_HEADER, EPISODES_HEADER  # noqa: E402
from knoventra.memory.working import get_working_memory  # noqa: E402
from knoventra.tools.backend import McpTools  # noqa: E402

MACHINE = re.compile(r"(?<!\d)6\d{5}(?!\d)")  # demo machine serials, e.g. 610101
SKILL_ASK = re.compile(r"谁会(?:做)?|找谁|谁懂")
# Asking to file one, not asking about one ("上次那个求助单…" is a recall question).
HELP_ASK = re.compile(r"帮我提|提个?(?:求助)?单|提交求助|发起求助|帮我报修")
STATUS_ASK = re.compile(r"进度|我的求助单|我的转交|转交记录")
TRIED = re.compile(r"已经|试过|重启|重插|复位|更换过")
CLARIFY_MARK = "哪个机型"  # the server's machine-model question (agent.model_variants)


def _tool_names(msgs) -> dict[str, str]:
    return {c["id"]: c["name"] for m in msgs if isinstance(m, AIMessage) for c in m.tool_calls}


def _episodes(system: str) -> list[str]:
    """Bullets of the memory block; an episode's continuation lines are indented under it."""
    out: list[str] = []
    for line in system.split(EPISODES_HEADER, 1)[1].splitlines():
        if not line.strip():
            break  # the next context block starts after a blank line
        if line.startswith("- "):
            out.append(line[2:])
        elif line.startswith("  ") and out:
            out[-1] += "\n" + line.strip()
    return out


def _draft_missing(system: str) -> str:
    block = system.split(DRAFT_HEADER, 1)[1].split("\n\n", 1)[0]
    return next((line[3:] for line in block.splitlines() if line.startswith("还缺：")), "")


def _reply_to_tool(msgs, last: ToolMessage) -> AIMessage:
    """Answer from the tool that was actually called, never from words in its output."""
    tool = _tool_names(msgs).get(last.tool_call_id)
    text = last.content
    if tool == "lookup_skills":
        if text.startswith("NO_ACCESS"):
            return AIMessage("（演示用固定回答）技能表只对 Ops 部门开放，无法查询。")
        return AIMessage("（演示用固定回答）技能表查询结果：\n" + text)
    if tool == "request_help" and text.startswith("求助单草稿已更新"):
        need = text.split("还缺：", 1)[1].split("。", 1)[0]
        return AIMessage(f"（演示用固定回答）求助单还需要：{need}。请先告诉我第一项。")
    if tool in ("request_help", "escalate_to_expert", "get_escalation_status"):
        return AIMessage(text)  # filing / cancel acknowledgements and the status list, as-is
    if "[S1]" in text:
        return AIMessage("（演示用固定回答）请依据下列来源核对步骤 [S1]。" + (" [S1]" if "[S2]" in text else ""))
    return AIMessage("资料中没有可靠依据，无法回答。建议联系对应职能团队（Ops/HR/FIN/IT）。")


def script(msgs):
    last = msgs[-1]
    humans = [m.content for m in msgs if m.type == "human"]
    human = humans[-1]
    system = msgs[0].content if msgs and msgs[0].type == "system" else ""
    if isinstance(last, ToolMessage):
        return _reply_to_tool(msgs, last)
    if "上次" in human:
        if EPISODES_HEADER in system:
            # Only episodes that passed the server's re-validation reach this block.
            return AIMessage("（演示用固定回答）根据你以往的记录：\n" + "\n".join(_episodes(system)))
        return call("search_knowledge", {"query": human})  # nothing recalled: never a new request
    if SKILL_ASK.search(human):
        skill = SKILL_ASK.split(human, 1)[-1].strip(" ？?。") or human
        return call("lookup_skills", {"skill": skill})
    draft_open = DRAFT_HEADER in system
    if HELP_ASK.search(human) or draft_open:
        args = {}
        machine = MACHINE.search(human)
        if machine:
            args["machine_id"] = machine.group()
        if not draft_open:
            args["symptom"] = human[:500]
            if TRIED.search(human):
                args["tried_steps"] = human[:500]
        elif "已尝试的步骤" in _draft_missing(system) and (machine is None or human.strip() != machine.group()):
            # The draft is waiting for tried steps: this answer is them, whatever its wording.
            args["tried_steps"] = human[:500]
        return call("request_help", args)
    if STATUS_ASK.search(human):
        return call("get_escalation_status", {}, "status")
    if "转人工" in human or "转交" in human:
        return call("escalate_to_expert", {"summary": human})
    # Only an answer to the machine-model question keeps the previous question as context.
    previous_reply = next((m.content for m in reversed(msgs[:-1])
                           if isinstance(m, AIMessage) and not m.tool_calls), "")
    query = f"{humans[-2]} {human}" if len(humans) > 1 and CLARIFY_MARK in previous_reply else human
    return call("search_knowledge", {"query": query})


settings.gate_threshold = 0.1  # hash-encoder cosine scale (the BGE-M3 default is 0.60)
settings.gate_sparse_threshold = 1e9
settings.episode_min_score = 0.3  # same reason: lexical hash vectors, not semantic ones
saver, _ = make_checkpointer(settings.database_url)
# Same transports as the real API: Redis working memory and MCP tools when configured.
tools = McpTools(settings.mcp_url, settings.mcp_timeout_s) if settings.mcp_url else None
graph = build_graph(AgentDeps(_session, get_encoder(), None, ScriptedModel(script=script), saver,
                              memory=get_working_memory(), tools=tools))
app.dependency_overrides[get_graph] = lambda: graph

if __name__ == "__main__":
    uvicorn.run(app, port=8765)
