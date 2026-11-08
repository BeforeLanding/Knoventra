"""Dense + sparse encoders.

`HashEncoder` is deterministic and offline (tests, CI). `BgeM3Encoder` loads the real
model through FlagEmbedding; it is imported lazily so the base install stays light.
Sparse vectors are `{token_id: weight}` dicts over a `sparse_dim`-sized vocabulary.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from numbers import Integral, Real
from typing import Protocol
from threading import Lock

from knoventra.config import ModelConfigurationError, settings

SparseVec = dict[int, float]
_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")


class EncoderOutputError(ValueError):
    """The encoder response cannot be safely passed to vector storage/search."""


def encode_checked(enc: Encoder, texts: list[str]):
    rows = enc.encode(texts)
    if not isinstance(rows, (list, tuple)):
        raise EncoderOutputError("encoder results must be a list or tuple")
    if len(rows) != len(texts):
        raise EncoderOutputError("encoder result count does not match requested texts")
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise EncoderOutputError("encoder results must contain dense/sparse pairs")
        dense, sparse = row
        try:
            if isinstance(dense, (str, bytes, Mapping)) or len(dense) != settings.dense_dim:
                raise EncoderOutputError("encoder dense vector dimension does not match configuration")
            for value in dense:
                if (isinstance(value, bool) or not isinstance(value, Real)
                        or abs(value) > _FLOAT32_MAX or not math.isfinite(value)):
                    raise EncoderOutputError("encoder dense values must be finite float32 numbers")
        except TypeError:
            raise EncoderOutputError("encoder dense vector must be a numeric sequence") from None
        if not isinstance(sparse, Mapping):
            raise EncoderOutputError("encoder sparse vector must be a mapping")
        for index, value in sparse.items():
            if (isinstance(index, bool) or not isinstance(index, Integral)
                    or not 0 <= index < settings.sparse_dim):
                raise EncoderOutputError("encoder sparse index is outside configured vocabulary")
            if (isinstance(value, bool) or not isinstance(value, Real) or not 0 <= value <= _FLOAT32_MAX
                    or not math.isfinite(value)):
                raise EncoderOutputError("encoder sparse weights must be finite nonnegative float32 numbers")
    return rows

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
        self.model_name = model_name
        self.model_revision = getattr(self.model.model.config, "_commit_hash", None)

    def encode(self, texts):
        if not texts:
            return []
        r = self.model.encode(texts, return_dense=True, return_sparse=True, return_colbert_vecs=False,
                              batch_size=16, max_length=512)
        try:
            if len(r["dense_vecs"]) != len(texts) or len(r["lexical_weights"]) != len(texts):
                raise EncoderOutputError("encoder result count does not match requested texts")
            return [([float(x) for x in dense], {int(k): float(v) for k, v in lw.items()})
                    for dense, lw in zip(r["dense_vecs"], r["lexical_weights"], strict=True)]
        except EncoderOutputError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError):
            raise EncoderOutputError("encoder response has invalid structure or numeric values") from None


_cached: Encoder | None = None
_cached_spec: tuple[str, int, int] | None = None
_load_lock = Lock()


def _encoder_spec() -> tuple[str, int, int]:
    if not isinstance(settings.encoder, str) or settings.encoder not in {"hash", "bge-m3"}:
        raise ModelConfigurationError("encoder must be hash or bge-m3")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (settings.dense_dim, settings.sparse_dim)):
        raise ModelConfigurationError("encoder dimensions must be positive integers")
    return settings.encoder, settings.dense_dim, settings.sparse_dim


def get_encoder() -> Encoder:
    global _cached, _cached_spec
    with _load_lock:
        spec = _encoder_spec()
        if _cached is not None:
            if _cached_spec != spec:
                raise ModelConfigurationError("loaded encoder configuration changed; restart the process")
            return _cached
        mode, dense_dim, sparse_dim = spec
        encoder = BgeM3Encoder() if mode == "bge-m3" else HashEncoder(dense_dim=dense_dim, sparse_dim=sparse_dim)
        if _encoder_spec() != spec:
            raise ModelConfigurationError("encoder configuration changed during loading; restart the process")
        _cached, _cached_spec = encoder, spec
        return encoder
