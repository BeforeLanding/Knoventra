from collections.abc import Callable
from contextlib import contextmanager

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    """Calls `script(messages) -> AIMessage`; tool binding is a no-op."""

    script: Callable[[list[BaseMessage]], AIMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=self.script(messages))])


def call(name, args, cid="c1"):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])


def session_factory_for(db):
    @contextmanager
    def f():
        yield db
    return f


class DictWorkingMemory:
    """In-process stand-in for RedisWorkingMemory; `down=True` simulates an outage."""

    def __init__(self):
        self.data: dict[str, dict] = {}
        self.down = False

    def _check(self):
        from knoventra.memory.working import WorkingMemoryUnavailable

        if self.down:
            raise WorkingMemoryUnavailable("simulated outage")

    def load(self, user_id, thread_id):
        self._check()
        return dict(self.data.get(f"{user_id}:{thread_id}", {}))

    def save(self, user_id, thread_id, fields):
        self._check()
        entry = self.data.setdefault(f"{user_id}:{thread_id}", {})
        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
