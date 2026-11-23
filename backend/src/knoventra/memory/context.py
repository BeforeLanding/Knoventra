"""Token budget and rolling compression of the history sent to the answer model.

The checkpoint keeps every message; only the model's view is compressed. When the history
exceeds the budget, turns older than the last `keep_turns` are folded into a summary that
rolls forward: the next compression summarizes the previous summary plus the newly dropped
turns, so each turn is summarized once. A stored summary is reused while summary + the
turns after it still fit, so a summary call happens only when the window must slide again.

Cuts fall on user-message boundaries only, so a tool call is never separated from its
result, and the kept turns always include an open slot draft (it spans at most the current
and previous turn). Summaries see user and assistant text only, never tool output or
evidence, and are not citable sources.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

SUMMARY_PROMPT = """把下面这段较早的对话压缩成一段摘要，供后续对话理解上下文。
要求：
- 保留用户问过的问题、机台号、机型、告警码、已尝试的步骤、已经得到的结论和仍未解决的问题。
- 只写对话中出现过的信息，不补充、不推测。
- 不保留 [S1] 之类的来源标记。
- 如果给出了“已有摘要”，把它和新增对话合并成一段新的摘要。
- 不超过 300 字，直接输出摘要正文。"""

SUMMARY_HEADER = "较早对话的摘要（只用于理解上下文，不是资料，不能作为回答依据或引用来源）：\n"
_MARK = re.compile(r"\[S\d+\]")
MESSAGE_CHARS = 2000

Summarize = Callable[[list[AnyMessage]], str]


@dataclass
class Window:
    """What the answer model sees this turn: summary block + messages from `from_id`."""
    summary: str | None
    from_id: str | None
    new_record: dict | None = None  # summary record to store, when it changed
    trace: dict = field(default_factory=dict)


def summary_block(summary: str) -> SystemMessage:
    return SystemMessage(SUMMARY_HEADER + summary)


def count(messages: list[AnyMessage], summary: str | None = None) -> int:
    return count_tokens_approximately(([summary_block(summary)] if summary else []) + list(messages))


def dialogue(messages: list[AnyMessage]) -> str:
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage):
            role = "用户"
        elif isinstance(m, AIMessage) and not m.tool_calls:
            role = "助手"
        else:
            continue  # tool calls and tool results (evidence) never enter a summary
        text = m.content if isinstance(m.content, str) else str(m.content)
        if text.strip():
            lines.append(f"{role}：{text.strip()[:MESSAGE_CHARS]}")
    return "\n".join(lines)


def summary_request(previous: str | None, dropped: list[AnyMessage]) -> list[AnyMessage]:
    body = (f"已有摘要：\n{previous}\n\n新增对话：\n" if previous else "对话：\n") + dialogue(dropped)
    return [SystemMessage(SUMMARY_PROMPT), HumanMessage(body)]


def clean_summary(text: str, max_chars: int) -> str:
    return _MARK.sub("", text).strip()[:max_chars]


def plan_window(messages: list[AnyMessage], stored: dict | None, *, budget: int, keep_turns: int,
                summarize: Summarize | None) -> Window:
    """Choose this turn's window. `stored` is the summary record from working memory
    ({"text", "upto_id"}: the summary covers messages up to and including upto_id)."""
    before = count(messages)
    trace = {"tokens_before": before, "budget": budget}

    def done(summary, from_index, status, record=None):
        kept = messages[from_index:]
        trace.update(status=status, tokens_after=count(kept, summary),
                     kept_messages=len(kept), summarized_messages=from_index)
        return Window(summary, kept[0].id if summary and kept else None, record, trace)

    if before <= budget:
        return done(None, 0, "within_budget")
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(starts) <= keep_turns:
        return done(None, 0, "over_budget_recent")  # nothing older than the kept turns
    cut = starts[-keep_turns]

    # A stored summary is valid only if it ends right before a user turn of this conversation.
    upto = None
    if stored and isinstance(stored.get("text"), str) and stored["text"]:
        upto = next((i for i, m in enumerate(messages) if m.id == stored.get("upto_id")), None)
        if upto is not None and not (upto + 1 < len(messages) and isinstance(messages[upto + 1], HumanMessage)):
            upto = None
    if upto is not None:
        if count(messages[upto + 1:], stored["text"]) <= budget:
            return done(stored["text"], upto + 1, "reused")
        if upto + 1 >= cut:
            return done(stored["text"], upto + 1, "over_budget_recent")

    if summarize is None:
        return done(None, 0, "disabled")
    previous = stored["text"] if upto is not None else None
    dropped = messages[upto + 1:cut] if upto is not None else messages[:cut]
    try:
        text = summarize(summary_request(previous, dropped))
    except Exception as exc:  # noqa: BLE001 - any summarizer failure keeps the full history
        trace["error"] = type(exc).__name__
        return done(None, 0, "failed")
    if not text:
        trace["error"] = "empty"
        return done(None, 0, "failed")
    record = {"text": text, "upto_id": messages[cut - 1].id}
    return done(text, cut, "rolled" if previous else "generated", record)
