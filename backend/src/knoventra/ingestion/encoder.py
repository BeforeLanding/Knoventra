"""Dense + sparse encoders.

`HashEncoder` is deterministic and offline (tests, CI). `BgeM3Encoder` loads the real
model through FlagEmbedding; it is imported lazily so the base install stays light.
Sparse vectors are `{token_id: weight}` dicts over a `sparse_dim`-sized vocabulary.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from knoventra.config import settings

SparseVec = dict[int, float]

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]*|[一-鿿]")


class Encoder(Protocol):
    def encode(self, texts: list[str]) -> list[tuple[list[float], SparseVec]]: ...


def _h(s: str, mod: int) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") % mod


class HashEncoder:
    """Bag-of-hashed-tokens; exact tokens (e.g. `E17-42`) match exactly in the sparse channel."""

    def __init__(self, dense_dim: int | None = None, sparse_dim: int | None = None):
        self.dd = dense_dim or settings.dense_dim
        self.sd = sparse_dim or settings.sparse_dim

    def _tokens(self, text: str) -> list[str]:
        toks = [t.lower() for t in _TOKEN.findall(text)]
        # character bigrams give the dense channel some fuzzy overlap
        return toks + [a + b for a, b in zip(toks, toks[1:])]

    def encode(self, texts):
        out = []
        for t in texts:
            toks = self._tokens(t)
            dense = [0.0] * self.dd
            sparse: SparseVec = {}
            for tok in toks:
                dense[_h("d" + tok, self.dd)] += 1.0
            for tok in _TOKEN.findall(t):
                sparse[_h("s" + tok.lower(), self.sd)] = sparse.get(_h("s" + tok.lower(), self.sd), 0.0) + 1.0
            n = math.sqrt(sum(x * x for x in dense)) or 1.0
            out.append(([x / n for x in dense], sparse))
        return out


class BgeM3Encoder:
    def __init__(self, model_name: str = "BAAI/bge-m3"):
        from FlagEmbedding import BGEM3FlagModel  # lazy: needs the `ml` extra

        self.model = BGEM3FlagModel(model_name, use_fp16=True)

    def encode(self, texts):
        r = self.model.encode(texts, return_dense=True, return_sparse=True, return_colbert_vecs=False,
                              batch_size=16, max_length=512)
        out = []
        for dense, lw in zip(r["dense_vecs"], r["lexical_weights"]):
            out.append(([float(x) for x in dense], {int(k): float(v) for k, v in lw.items()}))
        return out


_cached: Encoder | None = None


def get_encoder() -> Encoder:
    global _cached
    if _cached is None:
        _cached = BgeM3Encoder() if settings.encoder == "bge-m3" else HashEncoder()
    return _cached
