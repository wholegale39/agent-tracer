"""End-to-end test for Agent Call Tracer."""
import httpx
import sys

BASE = "http://localhost:8770"


def test_full_trace():
    c = httpx.Client(base_url=BASE)

    # 1. Create trace
    r = c.post("/traces", json={"agent": "market-bot", "task": "收盘汇总", "session_id": "s1"})
    assert r.status_code == 200
    trace_id = r.json()["trace_id"]
    print(f"✅ 1. Trace created: {trace_id}")

    # 2. Add spans (simulate a market summary workflow)
    spans = [
        {"tool_name": "web_search", "arguments": {"query": "上证指数"}, "result": "3200.15", "duration_ms": 850},
        {"tool_name": "web_search", "arguments": {"query": "恒生指数"}, "result": "18500.00", "duration_ms": 720},
        {"tool_name": "api_call", "arguments": {"url": "https://api.antfin.com/market", "method": "GET"},
         "result": '{"code":0,"data":{"sp":3200}}', "duration_ms": 350},
        {"tool_name": "terminal", "arguments": {"command": "python3 gen_chart.py"},
         "error": "timeout after 30s", "duration_ms": 30500},
    ]
    for i, s in enumerate(spans):
        r = c.post(f"/traces/{trace_id}/spans", json=s)
        assert r.status_code == 200
        print(f"✅ 2.{i+1} Span added: {s['tool_name']} ({r.json()['span_id']})")

    # 3. Finish trace
    r = c.post(f"/traces/{trace_id}/finish")
    assert r.status_code == 200
    print(f"✅ 3. Trace finished")

    # 4. Get full trace
    r = c.get(f"/traces/{trace_id}")
    assert r.status_code == 200
    t = r.json()
    assert t["span_count"] == 4
    assert t["spans"][3]["tool_name"] == "terminal"
    assert t["spans"][3]["error"] is not None
    print(f"✅ 4. Full trace: {t['span_count']} spans, last has error")

    # 5. List errors
    r = c.get("/spans/errors")
    errors = r.json()
    assert len(errors) >= 1
    assert errors[0]["tool_name"] == "terminal"
    print(f"✅ 5. Error spans: {len(errors)} found")

    # 6. Replay (the failed terminal call)
    r = c.post("/spans/4/replay", json={
        "span_id": 4,
        "arguments_override": {"command": "python3 gen_chart.py --timeout 60"},
    })
    replay = r.json()
    assert replay["original_tool"] == "terminal"
    assert "gen_chart.py --timeout 60" in replay["replay_instructions"]
    print(f"✅ 6. Replay instructions generated")
    print(f"    Original: {replay['original_arguments']['command']}")
    print(f"    Replay:   {replay['replay_instructions']}")

    # 7. List traces
    r = c.get("/traces")
    traces = r.json()
    assert len(traces) >= 1
    print(f"✅ 7. Traces listed: {len(traces)} total")

    print(f"\n🎉 ALL TESTS PASSED")


if __name__ == "__main__":
    test_full_trace()
