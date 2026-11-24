"""Slot collection for a field help request (draft storage: knoventra.memory.drafts).

The model proposes slot values through `request_help`; the server merges them,
derives alarm codes and the machine model deterministically, and decides what
is still missing. Nothing is created until the user confirms the full card.

Slot values must come from the user. The model also sees retrieved evidence, and a
historical case's steps are not what this user has tried, so every free-text value is
checked against the user's own messages before it is accepted: a machine number or alarm
code must appear there, and a symptom or tried-steps text must be mostly made of the
user's words (Chinese character pairs and Latin tokens). Unsupported values are dropped
and reported back, so the model asks instead of guessing.
"""
from __future__ import annotations

import re

from langchain_core.messages import AnyMessage, HumanMessage

from knoventra.agent.model_variants import stated_models
from knoventra.ingestion.cases import MODEL_ALIASES, alarm_codes
from knoventra.ingestion.normalize import to_simplified
from knoventra.retrieval.identifiers import extract_identifiers, normalize_identifiers

REQUIRED = ("machine_id", "symptom", "tried_steps")
LABELS = {"machine_id": "机台号", "machine_model": "机型", "alarm_codes": "告警码", "symptom": "故障现象",
          "tried_steps": "已尝试的步骤"}
# The whole message must be a cancel phrase (optionally with a particle or thanks), so a slot
# answer that merely starts with 取消 (e.g. "取消了 interlock 后重启") never cancels the draft.
CANCEL = re.compile(r"^\s*(算了|不用了|不要了|取消|先不用|不提了|cancel)(吧|啊|了|呀)?(求助单?|这个单子?)?"
                    r"\s*[，,]?\s*(谢谢|thanks)?\s*[。！!,.，~～]*\s*$", re.IGNORECASE)
MAX_SLOT_CHARS = 500
# Share of a free-text value's units that must occur in the user's messages. Paraphrase keeps
# most character pairs ("清洁过 chuck pin" -> "清洁 chuck pin"); copied evidence does not.
MIN_USER_SUPPORT = 0.5
_LATIN = re.compile(r"[a-z0-9][a-z0-9\-_.]*")
_CJK_RUN = re.compile(r"[一-鿿]+")


def _user_text(messages: list[AnyMessage]) -> str:
    return "\n".join(m.content if isinstance(m.content, str) else str(m.content)
                     for m in messages if isinstance(m, HumanMessage))


def _norm(text: str) -> str:
    return to_simplified(normalize_identifiers(text)).lower()


def _units(text: str) -> set[str]:
    norm = _norm(text)
    units = {t for t in _LATIN.findall(norm) if len(t) > 1}
    for run in _CJK_RUN.findall(norm):
        units |= {run[i:i + 2] for i in range(len(run) - 1)} if len(run) > 1 else {run}
    return units


def user_support(value: str, user_text: str) -> float:
    """Share of the value's units found in the user's own words (1.0 for a value with none)."""
    units = _units(value)
    return len(units & _units(user_text)) / len(units) if units else 1.0


def merge(draft: dict | None, args: dict, messages: list[AnyMessage]) -> dict:
    return merge_checked(draft, args, messages)[0]


def merge_checked(draft: dict | None, args: dict, messages: list[AnyMessage]) -> tuple[dict, list[str]]:
    """(merged draft, slot keys whose proposed values the user's messages do not support)."""
    out = dict(draft or {})
    user = _user_text(messages)
    user_norm = _norm(user)
    rejected: list[str] = []
    machine = (args.get("machine_id") or "").strip()
    if machine:
        if _norm(machine) in user_norm:
            out["machine_id"] = machine[:MAX_SLOT_CHARS]
        else:
            rejected.append("machine_id")
    for key in ("symptom", "tried_steps"):
        value = (args.get(key) or "").strip()
        if not value:
            continue
        if user_support(value, user) >= MIN_USER_SUPPORT:
            out[key] = value[:MAX_SLOT_CHARS]
        else:
            rejected.append(key)
    # Alarm codes: from the alarm field or the accepted symptom, but only codes the user wrote.
    user_codes = set(extract_identifiers(user)) | set(alarm_codes(user))
    proposed = {c for c in extract_identifiers(args.get("alarm") or "")
                if c not in MODEL_ALIASES and not c.startswith("SOP-")}
    proposed |= set(alarm_codes(out.get("symptom") or ""))
    if proposed - user_codes:
        rejected.append("alarm_codes")
    out["alarm_codes"] = sorted(set(out.get("alarm_codes") or []) | (proposed & user_codes))
    models = stated_models(messages)
    if len(models) == 1:
        out["machine_model"] = models[0]
    return out, rejected


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
