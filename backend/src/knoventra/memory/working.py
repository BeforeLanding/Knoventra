"""Short-term working memory: one Redis hash per conversation, `knv:wm:{user_id}:{thread_id}`.

Fields are JSON values (`draft`, `summary`). Every read and write renews the TTL, so an idle
conversation's memory expires on its own. Redis is a cache: callers validate what they read
against the checkpoint and rebuild on a miss, so losing Redis (restart, eviction, outage)
costs time, never correctness. Errors surface as `WorkingMemoryUnavailable`.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import redis

from knoventra.config import settings


class WorkingMemoryUnavailable(RuntimeError):
    pass


class RedisWorkingMemory:
    def __init__(self, client: redis.Redis, ttl_s: int):
        self.client = client
        self.ttl_s = ttl_s

    @staticmethod
    def key(user_id: str, thread_id: str) -> str:
        return f"knv:wm:{user_id}:{thread_id}"

    def load(self, user_id: str, thread_id: str) -> dict[str, Any]:
        key = self.key(user_id, thread_id)
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.hgetall(key)
            pipe.expire(key, self.ttl_s)
            raw, _ = pipe.execute()
        except redis.RedisError as exc:
            raise WorkingMemoryUnavailable(str(exc)) from exc
        out = {}
        for field, value in raw.items():
            try:
                out[field.decode()] = json.loads(value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # a corrupt field is a miss; the caller rebuilds it
        return out

    def save(self, user_id: str, thread_id: str, fields: dict[str, Any]) -> None:
        """Set fields; a None value deletes the field."""
        key = self.key(user_id, thread_id)
        sets = {k: json.dumps(v, ensure_ascii=False) for k, v in fields.items() if v is not None}
        dels = [k for k, v in fields.items() if v is None]
        try:
            pipe = self.client.pipeline(transaction=True)
            if sets:
                pipe.hset(key, mapping=sets)
            if dels:
                pipe.hdel(key, *dels)
            pipe.expire(key, self.ttl_s)
            pipe.execute()
        except redis.RedisError as exc:
            raise WorkingMemoryUnavailable(str(exc)) from exc


@lru_cache(maxsize=1)
def get_working_memory() -> RedisWorkingMemory | None:
    if not settings.redis_url:
        return None
    client = redis.Redis.from_url(settings.redis_url, socket_timeout=settings.redis_timeout_s,
                                  socket_connect_timeout=settings.redis_timeout_s)
    return RedisWorkingMemory(client, settings.working_memory_ttl_s)
