"""Field-help slot drafts: working copy in Redis, durable record in the checkpoint.

The checkpoint records every draft change where it already lives: each `request_help`
result carries the merged draft as its ToolMessage artifact (never sent to the model),
the confirmation card carries the full slots in `pending`, and filing, declining or
cancelling leaves an explicit "cleared" marker. The Redis copy is stamped with the anchor
(the id of the message that produced it); a read whose stamp does not match the newest
anchor in the checkpoint is stale or lost, and the draft is rebuilt from the checkpoint.

A draft only lives while consecutive turns keep working on it, so only the current and the
previous turn are searched: a turn spent on something else ends the draft.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

ARTIFACT_KEY = "help_draft"
CLEARED_FLAG = "help_draft_cleared"  # response_metadata on the cancel reply; not sent to the model


def checkpoint_draft(messages: list[AnyMessage], pending: dict | None) -> tuple[str | None, dict | None]:
    """(anchor, draft) as recorded in the checkpoint; (None, None) when no draft is open."""
    if pending and pending.get("kind") == "field_help" and pending.get("slots"):
        return f"card:{pending['operation_id']}", pending["slots"]
    humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = humans[-2] if len(humans) >= 2 else 0
    for m in reversed(messages[start:]):
        if isinstance(m, ToolMessage) and isinstance(m.artifact, dict) and ARTIFACT_KEY in m.artifact:
            draft = m.artifact[ARTIFACT_KEY]
            return (m.id, draft) if draft else (None, None)
        if isinstance(m, AIMessage) and m.response_metadata.get(CLEARED_FLAG):
            return None, None
    return None, None


def resolve(cached: Any, anchor: str | None, durable: dict | None) -> tuple[dict | None, str, bool]:
    """Pick the draft for this read: (draft, status, write_back).

    status: "none" (no open draft), "hit" (Redis copy matches the checkpoint) or "rebuilt".
    write_back is True when Redis should be repaired (stale entry or a miss).
    """
    if anchor is None:
        return None, "none", cached is not None  # a stale entry (e.g. a failed clear) is dropped
    if isinstance(cached, dict) and cached.get("anchor") == anchor and isinstance(cached.get("value"), dict):
        return cached["value"], "hit", False
    return durable, "rebuilt", True
