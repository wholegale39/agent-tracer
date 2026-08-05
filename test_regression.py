"""Unit tests for the trace regression engine (pure logic, no server)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.regression import compare, suggest_case_name, trace_signature
from src.models import Span, Trace


def make_trace(spans_spec, agent="market-bot", task="收盘汇总"):
    """Build a Trace from [(tool, args, error)] specs."""
    spans = []
    for i, (tool, args, error) in enumerate(spans_spec, start=1):
        spans.append(Span(
            id=i, trace_id="t", sequence=i,
            tool_name=tool, arguments=args,
            result=None if error else "ok",
            error=error, duration_ms=100,
        ))
    return Trace(id="t", agent=agent, task=task, spans=spans)


GOLDEN = [("web_search", {"query": "上证指数"}, None),
          ("api_call", {"url": "https://api.example.com/market", "method": "GET"}, None),
          ("terminal", {"command": "python3 gen_chart.py"}, None)]


def test_signature_shape():
    t = make_trace(GOLDEN)
    sig = trace_signature(t)
    assert sig["span_count"] == 3
    assert sig["tool_sequence"] == ["web_search", "api_call", "terminal"]
    assert sig["spans"][0] == {"tool": "web_search", "arg_keys": ["query"], "status": "ok"}
    assert sig["spans"][1]["arg_keys"] == ["method", "url"]


def test_match():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace(GOLDEN)
    r = compare(golden_sig, new)
    assert r.verdict == "match", r
    assert r.score == 1.0
    assert r.diffs == []


def test_drift_arg_keys():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace([("web_search", {"query": "上证", "lang": "zh"}, None),
                      ("api_call", {"url": "https://api.example.com/market", "method": "GET"}, None),
                      ("terminal", {"command": "python3 gen_chart.py"}, None)])
    r = compare(golden_sig, new)
    assert r.verdict == "drift", r
    kinds = [d.kind for d in r.diffs]
    assert "arg_keys_changed" in kinds
    assert "status_flipped" not in kinds
    assert r.score == round(2 / 3, 2)


def test_regression_status_flip():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace([("web_search", {"query": "上证指数"}, None),
                      ("api_call", {"url": "https://api.example.com/market", "method": "GET"},
                       "timeout after 30s"),
                      ("terminal", {"command": "python3 gen_chart.py"}, None)])
    r = compare(golden_sig, new)
    assert r.verdict == "regression", r
    assert any(d.kind == "status_flipped" for d in r.diffs)
    assert "timeout" in r.summary


def test_regression_removed_span():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace([("web_search", {"query": "上证指数"}, None),
                      ("terminal", {"command": "python3 gen_chart.py"}, None)])
    r = compare(golden_sig, new)
    assert r.verdict == "regression", r
    assert any(d.kind == "span_removed" for d in r.diffs)


def test_added_span_is_drift():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace(GOLDEN + [("web_search", {"query": "额外查询"}, None)])
    r = compare(golden_sig, new)
    assert r.verdict == "drift", r
    assert any(d.kind == "span_added" for d in r.diffs)


def test_tool_changed_is_drift():
    golden_sig = trace_signature(make_trace(GOLDEN))
    new = make_trace([("web_search", {"query": "上证指数"}, None),
                      ("web_search", {"query": "备用查询"}, None),
                      ("terminal", {"command": "python3 gen_chart.py"}, None)])
    r = compare(golden_sig, new)
    assert r.verdict == "drift", r
    assert any(d.kind == "tool_changed" for d in r.diffs)


def test_suggest_case_name():
    assert suggest_case_name("market-bot", "收盘汇总", "4bd366dc7e0d") == "market-bot/收盘汇总 · 4bd366dc"


def test_error_in_golden_is_allowed():
    """If the golden trace itself had an error span, that position expects error."""
    golden = [("web_search", {"query": "上证指数"}, None),
              ("terminal", {"command": "gen_chart.py"}, "timeout after 30s")]
    golden_sig = trace_signature(make_trace(golden))
    # same trace again → match, because golden expects the error
    r = compare(golden_sig, make_trace(golden))
    assert r.verdict == "match", r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
