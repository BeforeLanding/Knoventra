from __future__ import annotations

import math
from dataclasses import replace
from functools import lru_cache
from numbers import Real
from typing import Protocol

import numpy as np

from knoventra.config import settings
from knoventra.retrieval.identifiers import extract_identifiers, normalize_identifiers
from knoventra.retrieval.search import Evidence


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class BgeReranker:
    def __init__(self, name: str = "BAAI/bge-reranker-v2-m3"):
        from FlagEmbedding import FlagReranker  # lazy: needs the `ml` extra

        self.model_name = name
        self.m = FlagReranker(name, use_fp16=True)

    def score(self, query, texts):
        if not texts:
            return []
        r = self.m.compute_score([[query, t] for t in texts], normalize=True)
        # FlagEmbedding may return a scalar, list, or NumPy array for a single pair.
        return np.asarray(r, dtype=float).reshape(-1).tolist()


@lru_cache(maxsize=1)
def _load_reranker(name: str) -> Reranker:
    return BgeReranker(name)


def get_reranker() -> Reranker | None:
    return _load_reranker(settings.rerank_model) if settings.rerank == "bge" else None


def rerank(rr: Reranker, query: str, ev: list[Evidence], top_k: int) -> list[Evidence]:
    if top_k < 1:
        raise ValueError("rerank top_k must be positive")
    if not ev:
        return []
    query = normalize_identifiers(query) if extract_identifiers(query) else query
    # Headings carry fault codes and context that may not be repeated in the body.
    passages = [f"{e.section_path}\n{e.text}" if e.section_path else e.text for e in ev]
    scores = rr.score(query, passages)
    if len(scores) != len(ev):
        raise ValueError("reranker must return one score per candidate")
    if any(not isinstance(s, Real) or isinstance(s, bool) or not math.isfinite(s) or not 0.0 <= s <= 1.0
           for s in scores):
        raise ValueError("reranker scores must be finite numbers in [0, 1]")
    ranked = [replace(e, rerank_score=float(s)) for e, s in zip(ev, scores)]
    return sorted(ranked, key=lambda e: e.rerank_score, reverse=True)[:top_k]
