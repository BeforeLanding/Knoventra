"""Elasticsearch keyword index: analysis, mapping and versioned index names.

The index is a derived copy of PostgreSQL. Text is stored OpenCC-simplified
with canonical identifiers because IK splits Traditional Chinese into single
characters. Spelling fixes run at index and query time; synonyms only at query
time (synonym_graph is a search-time filter).
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _rules(name: str) -> list[str]:
    lines = (_HERE / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


@lru_cache(maxsize=1)
def index_body() -> dict:
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "filter": {
                    "knv_typos": {"type": "synonym", "synonyms": _rules("typos.txt"), "lenient": True},
                    "knv_synonyms": {"type": "synonym_graph", "synonyms": _rules("synonyms.txt"), "lenient": True},
                },
                "analyzer": {
                    "knv_index": {"type": "custom", "tokenizer": "ik_max_word", "filter": ["lowercase", "knv_typos"]},
                    "knv_search": {"type": "custom", "tokenizer": "ik_smart",
                                   "filter": ["lowercase", "knv_typos", "knv_synonyms"]},
                },
                "normalizer": {"knv_upper": {"type": "custom", "filter": ["uppercase"]}},
            },
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "revision_id": {"type": "keyword"},
                "kb_id": {"type": "keyword"},
                "revision_status": {"type": "keyword"},
                "lang": {"type": "keyword"},
                "machine_model": {"type": "keyword"},
                "region": {"type": "keyword"},
                "identifiers": {"type": "keyword", "normalizer": "knv_upper"},
                "title": {"type": "text", "analyzer": "knv_index", "search_analyzer": "knv_search"},
                "section_path": {"type": "text", "analyzer": "knv_index", "search_analyzer": "knv_search"},
                "text": {
                    "type": "text", "analyzer": "knv_index", "search_analyzer": "knv_search",
                    # English terms get fuzzy matching here; fuzziness cannot run through graph synonyms.
                    "fields": {"latin": {"type": "text", "analyzer": "standard"}},
                },
                "published_at": {"type": "date"},
            },
        },
    }


def mapping_version() -> str:
    blob = json.dumps(index_body(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def physical_name(alias: str, suffix: str | None = None) -> str:
    """alias-<mapping hash>[-suffix]; a mapping change yields a new physical index."""
    return f"{alias}-{mapping_version()}" + (f"-{suffix}" if suffix else "")
