"""Elasticsearch keyword recall. Returns chunk ids only; PostgreSQL re-checks them.

The ES query carries the readable-KB and PUBLISHED filters itself, but ES is a
derived copy that can lag or be wrong, so callers must never trust its hits
without the PG back-check in `retrieval.search`.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from elasticsearch import Elasticsearch

from knoventra.config import settings
from knoventra.ingestion.normalize import to_simplified
from knoventra.retrieval.identifiers import normalize_query_identifiers


@dataclass(frozen=True)
class KeywordChannel:
    es: Elasticsearch
    alias: str

    def search(self, query: str, readable_kb_ids: list[str], identifiers: list[str], k: int) -> list[str]:
        q = to_simplified(normalize_query_identifiers(query))
        should: list[dict] = [
            {"match": {"text": {"query": q}}},
            {"match": {"section_path": {"query": q, "boost": 1.5}}},
            {"match": {"title": {"query": q, "boost": 1.2}}},
            # Latin-only subfield: typo tolerance for English terms the typo table does not cover.
            {"match": {"text.latin": {"query": q, "fuzziness": "AUTO", "prefix_length": 1, "boost": 0.5}}},
        ]
        if identifiers:
            should.insert(0, {"terms": {"identifiers": identifiers, "boost": 5.0}})
        resp = self.es.search(index=self.alias, size=k, source=False, track_total_hits=False, query={"bool": {
            "filter": [{"terms": {"kb_id": readable_kb_ids}}, {"term": {"revision_status": "PUBLISHED"}}],
            "should": should,
            "minimum_should_match": 1,
        }})
        return [h["_id"] for h in resp["hits"]["hits"]]


@lru_cache(maxsize=4)
def _channel(url: str, alias: str, timeout: float) -> KeywordChannel:
    return KeywordChannel(Elasticsearch(url, request_timeout=timeout, max_retries=0), alias)


def default_keyword_channel() -> KeywordChannel | None:
    """The configured channel, or None when KNV_ES_URL is empty (pgvector-only mode)."""
    if not settings.es_url:
        return None
    return _channel(settings.es_url, settings.es_index, settings.es_timeout_s)
