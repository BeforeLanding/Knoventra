"""Offline evaluation scope and scenario-cost planning; never loads models or DBs."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math


def select_cases(cases: list[dict], requested: list[str] | None) -> list[dict]:
    if not requested:
        return cases
    selected = set(requested)
    if selected - {c["id"] for c in cases}:
        raise ValueError("requested case id is absent from the selected split")
    return [c for c in cases if c["id"] in selected]


def plan(cases: list[dict], *, mode: str, split: str, tool_limit: int, model: str,
         max_output_tokens: int, assumed_input_tokens: int,
         input_price: float | None, output_price: float | None, currency: str) -> dict:
    if not cases:
        raise ValueError("no cases selected")
    if tool_limit < 0 or max_output_tokens < 1 or assumed_input_tokens < 1:
        raise ValueError("tool limit must be nonnegative; token limits must be positive")
    if (input_price is None) != (output_price is None):
        raise ValueError("supply both input and output prices, or neither")
    if any(p is not None and (not math.isfinite(p) or p < 0) for p in (input_price, output_price)):
        raise ValueError("prices must be finite and nonnegative")
    if not currency.strip():
        raise ValueError("currency must be nonempty")
    calls = len(cases) * (tool_limit + 1) if mode == "agent" else 0
    estimated = (calls * (assumed_input_tokens * input_price + max_output_tokens * output_price) / 1_000_000
                 if input_price is not None else None)
    if estimated is not None and not math.isfinite(estimated):
        raise ValueError("scenario cost estimate is not finite")
    return {"schema_version": 1, "mode": "preflight", "evaluation_mode": mode, "split": split,
            "case_ids": [c["id"] for c in cases], "n_cases": len(cases),
            "confirmation_decision_cases": [c["id"] for c in cases if c["category"] == "escalation"] if mode == "agent" else [],
            "categories": dict(sorted(Counter(c["category"] for c in cases).items())),
            "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "model": model or None, "max_tool_calls_per_turn": tool_limit,
            "graph_model_call_bound": calls, "automatic_provider_retries": 0,
            "max_output_tokens_per_call": max_output_tokens,
            "requested_output_token_bound": calls * max_output_tokens,
            "assumed_input_tokens_per_call": assumed_input_tokens,
            "input_price_per_million": input_price, "output_price_per_million": output_price,
            "currency": currency, "scenario_cost_estimate": round(estimated, 8) if estimated is not None else None,
            "limitation": "Scenario estimate, not a billing cap. Input length is assumed, not measured; "
                          "provider token semantics, reasoning/cache charges and failed requests may differ. "
                          "Output bound depends on provider honoring max_tokens. No calls made by preflight."}
