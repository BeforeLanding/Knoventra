from __future__ import annotations

from typing import Protocol

from knoventra.config import settings
from knoventra.retrieval.search import Evidence


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class BgeReranker:
    def __init__(self, name: str = "BAAI/bge-reranker-v2-m3"):
        from FlagEmbedding import FlagReranker  # lazy: needs the `ml` extra

        self.m = FlagReranker(name, use_fp16=True)

    def score(self, query, texts):
        r = self.m.compute_score([[query, t] for t in texts], normalize=True)
        return [float(r)] if isinstance(r, float) else [float(x) for x in r]


_cached: Reranker | None | bool = False


def get_reranker() -> Reranker | None:
    global _cached
    if _cached is False:
        _cached = BgeReranker() if settings.rerank == "bge" else None
    return _cached


def rerank(rr: Reranker, query: str, ev: list[Evidence], top_k: int) -> list[Evidence]:
    if not ev:
        return ev
    for e, s in zip(ev, rr.score(query, [e.text for e in ev])):
        e.rerank_score = s
    return sorted(ev, key=lambda e: e.rerank_score, reverse=True)[:top_k]
