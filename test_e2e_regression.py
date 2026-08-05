"""End-to-end test for Agent Call Tracer regression features (promote/check)."""
import httpx
import sys

BASE = "http://localhost:8770"


def _make_trace(c, agent, task, spans):
    r = c.post("/traces", json={"agent": agent, "task": task, "session_id": "e2e"})
    trace_id = r.json()["trace_id"]
    for s in spans:
        c.post(f"/traces/{trace_id}/spans", json=s)
    c.post(f"/traces/{trace_id}/finish")
    return trace_id


def test_regression_flow():
    c = httpx.Client(base_url=BASE, timeout=10)

    # 1. Golden trace: a healthy market-summary run
    good_spans = [
        {"tool_name": "web_search", "arguments": {"query": "上证指数"}, "result": "3200.15", "duration_ms": 850},
        {"tool_name": "api_call", "arguments": {"url": "https://api.example.com/market", "method": "GET"},
         "result": '{"code":0,"data":{"sp":3200}}', "duration_ms": 350},
        {"tool_name": "terminal", "arguments": {"command": "python3 gen_chart.py"}, "result": "chart.png", "duration_ms": 1200},
    ]
    golden_id = _make_trace(c, "market-bot", "收盘汇总", good_spans)
    print(f"✅ 1. Golden trace created: {golden_id}")

    # 2. Promote it into a golden regression case
    r = c.post(f"/traces/{golden_id}/promote")
    assert r.status_code == 200
    data = r.json()
    case_id = data["case_id"]
    assert data["spans"] == 3
    assert data["agent"] == "market-bot"
    print(f"✅ 2. Promoted → case {case_id}: {data['name']}")

    # 3. Identical trace → verdict match
    same_id = _make_trace(c, "market-bot", "收盘汇总", good_spans)
    r = c.post(f"/cases/{case_id}/check", params={"trace_id": same_id})
    data = r.json()
    assert data["verdict"] == "match", data
    assert data["score"] == 1.0
    print(f"✅ 3. Identical trace → match (score {data['score']})")

    # 4. Drift: arg keys changed on web_search
    drift_spans = [
        {"tool_name": "web_search", "arguments": {"query": "上证指数", "lang": "zh"}, "result": "3200.15", "duration_ms": 850},
        {"tool_name": "api_call", "arguments": {"url": "https://api.example.com/market", "method": "GET"},
         "result": '{"code":0,"data":{"sp":3200}}', "duration_ms": 350},
        {"tool_name": "terminal", "arguments": {"command": "python3 gen_chart.py"}, "result": "chart.png", "duration_ms": 1200},
    ]
    drift_id = _make_trace(c, "market-bot", "收盘汇总", drift_spans)
    r = c.post(f"/cases/{case_id}/check", params={"trace_id": drift_id})
    data = r.json()
    assert data["verdict"] == "drift", data
    assert any(d["kind"] == "arg_keys_changed" for d in data["diffs"])
    print(f"✅ 4. Arg drift → {data['verdict']} ({data['summary'][:80]})")

    # 5. Regression: api_call now fails
    reg_spans = [
        {"tool_name": "web_search", "arguments": {"query": "上证指数"}, "result": "3200.15", "duration_ms": 850},
        {"tool_name": "api_call", "arguments": {"url": "https://api.example.com/market", "method": "GET"},
         "error": "timeout after 30s", "duration_ms": 30500},
        {"tool_name": "terminal", "arguments": {"command": "python3 gen_chart.py"}, "result": "chart.png", "duration_ms": 1200},
    ]
    reg_id = _make_trace(c, "market-bot", "收盘汇总", reg_spans)
    r = c.post(f"/cases/{case_id}/check", params={"trace_id": reg_id})
    data = r.json()
    assert data["verdict"] == "regression", data
    assert any(d["kind"] == "status_flipped" for d in data["diffs"])
    print(f"✅ 5. api_call failure → {data['verdict'].upper()} (was ok, now failing)")

    # 6. Auto-check via trace endpoint (agent+task match)
    r = c.post(f"/traces/{reg_id}/check")
    data = r.json()
    assert data["found"] is True
    assert data["case_id"] == case_id
    assert data["verdict"] == "regression"
    print(f"✅ 6. Auto check trace → found case, verdict {data['verdict']}")

    # 7. List cases
    r = c.get("/cases")
    cases = r.json()
    assert len(cases) >= 1
    assert cases[0]["last_verdict"] == "regression"
    assert cases[0]["last_score"] == round(2 / 3, 2)
    print(f"✅ 7. Cases listed: {len(cases)}, last verdict persisted ({cases[0]['last_verdict']})")

    print(f"\n🎉 ALL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    test_regression_flow()
