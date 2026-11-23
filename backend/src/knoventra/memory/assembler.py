"""Context assembler: one budget, filled by priority, trimmed from the bottom.

Priority (high to low): system prompt > open slot draft > profile > episodic memories >
rolling summary > earlier kept turns > the current turn. Over budget, the lowest optional
part goes first: earlier turns (oldest first), then the summary, then episodes (weakest
first), then the profile. The system prompt, the slot draft and the current turn are never
dropped: the current turn holds the question and this turn's tool calls with their results,
and cutting a tool result would break tool-call pairing and the citations built on it
(its evidence is already bounded by final_top_k). The budget excludes the fixed system
prompt and tool schemas, which do not vary between turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from knoventra.memory.context import SUMMARY_HEADER, count

DRAFT_HEADER = "当前求助单草稿（服务端记录，确认卡会让用户逐项核对）：\n"
PROFILE_HEADER = "用户画像（只用于理解指代和追问，不代表本轮问题涉及这些机台，不影响权限）：\n"
EPISODES_HEADER = ("历史记忆（该用户以往对话和求助单的记录，已按当前权限复核；可用于回答“上次……”这类问题，"
                   "但不是资料，不能作为引用来源）：\n")


@dataclass
class Assembled:
    messages: list[AnyMessage]
    trace: dict = field(default_factory=dict)


def sections(*, draft: str | None, profile: str | None, episodes: list[str], summary: str | None) -> str:
    out = []
    if draft:
        out.append(DRAFT_HEADER + draft)
    if profile:
        out.append(PROFILE_HEADER + profile)
    if episodes:
        out.append(EPISODES_HEADER + "\n".join(f"- {e}" for e in episodes))
    if summary:
        out.append(SUMMARY_HEADER + summary)
    return "\n\n".join(out)


def memory_tokens(*, profile: str | None, episodes: list[str]) -> int:
    """Tokens the turn-start memory blocks take; the history window gets the rest of the budget."""
    text = sections(draft=None, profile=profile, episodes=episodes, summary=None)
    return count([SystemMessage(text)]) if text else 0


def assemble(system_prompt: str, messages: list[AnyMessage], *, budget: int, draft: str | None = None,
             profile: str | None = None, episodes: list[str] | None = None, summary: str | None = None) -> Assembled:
    episodes = list(episodes or [])
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    current = starts[-1] if starts else 0
    bounds = [0, *[s for s in starts if 0 < s < current], current] if current else [0]
    earlier = [messages[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]
    dropped: list[str] = []

    def size() -> int:
        extra = sections(draft=draft, profile=profile, episodes=episodes, summary=summary)
        kept = [m for turn in earlier for m in turn] + messages[current:]
        return count(([SystemMessage(extra)] if extra else []) + kept)

    before = size()
    while size() > budget:
        if earlier:
            earlier.pop(0)
            dropped.append("turn")
        elif summary:
            summary = None
            dropped.append("summary")
        elif episodes:
            episodes.pop()  # recalled best-first: the weakest goes first
            dropped.append("episode")
        elif profile:
            profile = None
            dropped.append("profile")
        else:
            break  # only pinned parts remain; send them over budget rather than cut a tool result
    extra = sections(draft=draft, profile=profile, episodes=episodes, summary=summary)
    system = SystemMessage(f"{system_prompt}\n\n{extra}" if extra else system_prompt)
    out = [system, *[m for turn in earlier for m in turn], *messages[current:]]
    return Assembled(out, {"event": "assemble", "budget": budget, "tokens_before": before, "tokens_after": size(),
                           "dropped": dropped, "over_budget": size() > budget})
