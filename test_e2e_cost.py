"""End-to-end API tests for the v0.4 cost & error-attribution endpoints."""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

from src import api
from src.store import TraceStore


@contextmanager
def _client(tmp_path):
    """TestClient backed by a throwaway SQLite DB (lifespan builds the store)."""
    real = api.TraceStore
    api.TraceStore = lambda: TraceStore(str(tmp_path / "t.db"))
    try:
        with TestClient(api.app) as c:
            yield c
    finally:
        api.TraceStore = real


def test_cost_endpoints_e2e(tmp_path):
    with _client(tmp_path) as c:
        # seed one trace with token-aware spans (incl. an error)
        tid = c.post("/traces", json={"agent": "market-bot", "task": "收盘汇总"}).json()["trace_id"]
        c.post(f"/traces/{tid}/spans", json={
            "tool_name": "api_call", "arguments": {"url": "https://a.example/x"},
            "model": "deepseek-v4-flash", "input_tokens": 500_000, "output_tokens": 100_000,
        })
        c.post(f"/traces/{tid}/spans", json={
            "tool_name": "api_call", "arguments": {"url": "https://a.example/x"},
            "model": "deepseek-v4-flash", "input_tokens": 500_000, "output_tokens": 100_000,
            "error": "timeout after 30s calling https://x/1",
        })
        c.post(f"/traces/{tid}/spans", json={
            "tool_name": "terminal", "arguments": {"command": "ls"},
            "model": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 200_000,
        })
        c.post(f"/traces/{tid}/finish")

        # per-trace cost
        r = c.get(f"/cost/traces/{tid}").json()
        assert r["totals"]["spans"] == 3
        assert r["totals"]["total_cost"] == round(0.28 + 0.084 + 0.15 + 0.12, 6)
        assert set(r["per_tool"]) == {"api_call", "terminal"}
        # gpt-4o-mini must NOT be priced as gpt-4o (regression guard)
        assert r["per_model"]["gpt-4o-mini"]["total_cost"] == 0.27

        # summary across traces
        s = c.get("/cost/summary").json()
        assert s["scoped_traces"] == 1
        assert s["totals"]["total_cost"] == round(0.28 + 0.084 + 0.15 + 0.12, 6)
        assert s["per_agent"]["market-bot"]["traces"] == 1
        assert s["per_model"]["deepseek-v4-flash"]["calls"] == 2

        # error attribution
        e = c.get("/errors/aggregate").json()
        assert e["total_error_spans"] == 1
        assert e["buckets"][0]["tool"] == "api_call"
        assert e["buckets"][0]["trace_ids"] == [tid]


def test_cost_summary_agent_filter_and_404s(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/cost/traces/nope").status_code == 404

        for agent in ("a", "b"):
            tid = c.post("/traces", json={"agent": agent, "task": "t"}).json()["trace_id"]
            c.post(f"/traces/{tid}/spans", json={
                "tool_name": "web_search", "model": "deepseek-v4-flash",
                "input_tokens": 100, "output_tokens": 10,
            })

        only_a = c.get("/cost/summary", params={"agent": "a"}).json()
        assert only_a["scoped_traces"] == 1
        assert set(only_a["per_agent"]) == {"a"}

        all_agents = c.get("/cost/summary").json()
        assert all_agents["scoped_traces"] == 2
        assert set(all_agents["per_agent"]) == {"a", "b"}
