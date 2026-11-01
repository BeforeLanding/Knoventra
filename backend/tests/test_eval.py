import collections
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import run_eval  # noqa: E402

MANIFEST = Path(__file__).resolve().parents[2] / "data" / "demo" / "manifest.json"


def test_case_set_shape():
    cases = run_eval.load_cases("all")
    assert len(cases) == 60 and len({c["id"] for c in cases}) == 60
    assert collections.Counter(c["category"] for c in cases) == {"ops": 20, "cross": 10, "access": 15, "no_basis": 10, "escalation": 5}
    assert len(run_eval.load_cases("dev")) == 20 and len(run_eval.load_cases("holdout")) == 40
    titles = {d["title"] for d in json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]}
    for c in cases:
        e = c["expect"]
        for t in [e.get("title"), *e.get("forbidden_titles", [])]:
            assert t is None or t in titles, (c["id"], t)


def test_holdout_requires_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_eval", "--split", "holdout"])
    with pytest.raises(SystemExit):
        run_eval.main()


def test_retrieval_eval_runs_and_has_zero_leaks(db, enc, users):
    cases = run_eval.load_cases("all")
    res = run_eval.eval_retrieval(db, enc, None, users, cases, use_sparse=True, use_opencc=True, threshold=0.6)
    m = run_eval.summarize(res)
    assert m["access"]["leaks"] == 0 and m["access"]["n"] == 15
    assert m["ops"]["n"] == 20 and m["cross"]["n"] == 10 and m["no_basis"]["n"] == 10


def test_agent_eval_escalation_counts(db, enc, users):
    from fakes import ScriptedModel, call, session_factory_for
    from langchain_core.messages import AIMessage, ToolMessage

    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.config import settings

    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(msgs[-1].content)
        return call("escalate_to_expert", {"summary": "s"}, cid="e1")

    saver, pool = make_checkpointer(settings.database_url)
    try:
        g = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
        rs = run_eval.eval_agent(g, db, users, [c for c in run_eval.load_cases("all") if c["category"] == "escalation"])
        assert all(r["ok"] for r in rs), rs
    finally:
        pool.close()
