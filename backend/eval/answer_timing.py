"""Observe streamed answer content without retaining prompts, tokens or provider metadata."""
from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

ANSWER_TIMING_POLICY = "last-model-content-chunk-v1"


class AnswerChunkTimer(BaseCallbackHandler):
    """Time the final non-tool model response, measured from the start of the case.

    Completion decides whether content belonged to an answer or a tool-call preamble.
    Non-streaming responses have no first-chunk measurement, even if they contain text.
    """

    run_inline = True
    raise_error = True

    def __init__(self, started_at: float, clock: Callable[[], float] = time.perf_counter):
        self.started_at = started_at
        self.clock = clock
        self._first: dict[UUID, float | None] = {}
        self.first_answer_chunk_ms: float | None = None

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        if (metadata or {}).get("langgraph_node") == "agent":
            self._first[run_id] = None
            self.first_answer_chunk_ms = None

    def on_llm_new_token(self, token, *, run_id, chunk=None, **kwargs):
        if run_id not in self._first or self._first[run_id] is not None:
            return
        if (isinstance(chunk, ChatGenerationChunk) and isinstance(chunk.message, AIMessageChunk)
                and isinstance(chunk.message.content, str) and chunk.message.content):
            self._first[run_id] = round((self.clock() - self.started_at) * 1000, 3)

    def on_llm_end(self, response, *, run_id, **kwargs):
        if run_id not in self._first:
            return
        first = self._first.pop(run_id)
        self.first_answer_chunk_ms = None
        if not response.generations or not response.generations[0]:
            return
        message = getattr(response.generations[0][0], "message", None)
        if (isinstance(message, AIMessage) and not message.tool_calls and not message.invalid_tool_calls
                and isinstance(message.content, str) and message.content
                and message.response_metadata.get("finish_reason") != "length"):
            self.first_answer_chunk_ms = first

    def on_llm_error(self, error, *, run_id, **kwargs):
        if run_id in self._first:
            self._first.pop(run_id)
            self.first_answer_chunk_ms = None
