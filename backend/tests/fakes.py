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
