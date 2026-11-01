"""Local UI check ONLY: runs the real app with a scripted fake LLM (no API key needed).

    KNV_ENCODER=hash KNV_GATE_THRESHOLD=0.6 uv run python dev/mock_server.py

Answers are canned, so this demonstrates UI/flow behaviour (sources, refusal, confirm), not answer quality.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import uvicorn  # noqa: E402
from fakes import ScriptedModel, call  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from knoventra.agent.checkpoint import make_checkpointer  # noqa: E402
from knoventra.agent.graph import AgentDeps, build_graph  # noqa: E402
from knoventra.api.chat import _session, get_graph  # noqa: E402
from knoventra.config import settings  # noqa: E402
from knoventra.ingestion.encoder import get_encoder  # noqa: E402
from knoventra.main import app  # noqa: E402


def script(msgs):
    last = msgs[-1]
    human = [m.content for m in msgs if m.type == "human"][-1]
    if isinstance(last, ToolMessage):
        if "转交记录" in last.content or "取消" in last.content:
            return AIMessage(last.content)
        if "[S1]" in last.content:
            return AIMessage("（演示用固定回答）请依据下列来源核对步骤 [S1]。" + (" [S1]" if "[S2]" in last.content else ""))
        return AIMessage("资料中没有可靠依据，无法回答。建议联系对应职能团队（Ops/HR/FIN/IT）。")
    if "转人工" in human or "转交" in human:
        return call("escalate_to_expert", {"summary": human})
    return call("search_knowledge", {"query": human})


settings.gate_threshold = 0.1  # hash-encoder cosine scale (the BGE-M3 default is 0.60)
settings.gate_sparse_threshold = 1e9
saver, _ = make_checkpointer(settings.database_url)
graph = build_graph(AgentDeps(_session, get_encoder(), None, ScriptedModel(script=script), saver))
app.dependency_overrides[get_graph] = lambda: graph

if __name__ == "__main__":
    uvicorn.run(app, port=8765)
