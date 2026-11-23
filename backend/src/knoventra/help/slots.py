"""Slot collection for a field help request (draft storage: knoventra.memory.drafts).

The model proposes slot values through `request_help`; the server merges them,
derives alarm codes and the machine model deterministically, and decides what
is still missing. Nothing is created until the user confirms the full card.
"""
from __future__ import annotations

import re

from langchain_core.messages import AnyMessage

from knoventra.agent.model_variants import stated_models
from knoventra.ingestion.cases import MODEL_ALIASES, alarm_codes
from knoventra.retrieval.identifiers import extract_identifiers

REQUIRED = ("machine_id", "symptom", "tried_steps")
LABELS = {"machine_id": "机台号", "machine_model": "机型", "alarm_codes": "告警码", "symptom": "故障现象",
          "tried_steps": "已尝试的步骤"}
# The whole message must be a cancel phrase (optionally with a particle or thanks), so a slot
# answer that merely starts with 取消 (e.g. "取消了 interlock 后重启") never cancels the draft.
CANCEL = re.compile(r"^\s*(算了|不用了|不要了|取消|先不用|不提了|cancel)(吧|啊|了|呀)?(求助单?|这个单子?)?"
                    r"\s*[，,]?\s*(谢谢|thanks)?\s*[。！!,.，~～]*\s*$", re.IGNORECASE)
MAX_SLOT_CHARS = 500


def merge(draft: dict | None, args: dict, messages: list[AnyMessage]) -> dict:
    out = dict(draft or {})
    for key in ("machine_id", "symptom", "tried_steps"):
        value = (args.get(key) or "").strip()
        if value:
            out[key] = value[:MAX_SLOT_CHARS]
    codes = set(out.get("alarm_codes") or [])
    # The alarm field may name any code; free-text symptoms only contribute real alarm codes,
    # never model names, SOP numbers or part numbers.
    codes |= {c for c in extract_identifiers(args.get("alarm") or "")
              if c not in MODEL_ALIASES and not c.startswith("SOP-")}
    codes |= set(alarm_codes(out.get("symptom") or ""))
    out["alarm_codes"] = sorted(codes)
    models = stated_models(messages)
    if len(models) == 1:
        out["machine_model"] = models[0]
    return out


def missing(draft: dict) -> list[str]:
    return [k for k in REQUIRED if not draft.get(k)]


def card(draft: dict) -> str:
    """Confirmation card text: every slot, so the user checks exactly what will be filed."""
    lines = ["现场求助单"]
    for key in ("machine_id", "machine_model", "alarm_codes", "symptom", "tried_steps"):
        value = draft.get(key)
        if isinstance(value, list):
            value = "、".join(value)
        lines.append(f"{LABELS[key]}：{value or '（未提供）'}")
    return "\n".join(lines)
