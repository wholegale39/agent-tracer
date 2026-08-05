"""Unit tests for v0.3 export features: pytest case generation + trace serialization."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.models import Span, Trace
from src.regression import export_pytest_case, suggest_case_name, trace_signature, trace_to_export_dict


def make_trace(spans_spec, agent="market-bot", task="收盘汇总", trace_id="abc123"):
    spans = []
    for i, (tool, args, error) in enumerate(spans_spec, start=1):
        spans.append(Span(
            id=i, trace_id=trace_id, sequence=i,
            tool_name=tool, arguments=args,
            result=None if error else "ok",
            error=error, duration_ms=100,
        ))
    return Trace(id=trace_id, agent=agent, task=task, spans=spans)


GOLDEN = [("web_search", {"query": "上证指数"}, None),
          ("api_call", {"url": "https://api.example.com/market", "method": "GET"}, None),
          ("terminal", {"command": "python3 gen_chart.py"}, None)]


def test_export_pytest_case_renders():
    trace = make_trace(GOLDEN)
    sig = trace_signature(trace)
    code = export_pytest_case(sig, "case123", "market-bot/收盘汇总", trace.id)

    assert "GOLDEN_SIGNATURE" in code
    assert "test_no_regression" in code
    assert "test_structure_matches_golden" in code
    assert "web_search" in code
    assert "market-bot" in code
    # must be valid python
    compile(code, "test_golden_case123.py", "exec")


def test_exported_pytest_runs_and_passes():
    """End-to-end: export the pytest file, feed it a matching trace JSON, run it."""
    trace = make_trace(GOLDEN)
    sig = trace_signature(trace)
    code = export_pytest_case(sig, "case123", "market-bot/收盘汇总", trace.id)

    with tempfile.TemporaryDirectory() as tmp:
        test_file = Path(tmp) / "test_golden_case123.py"
        test_file.write_text(code, encoding="utf-8")

        # matching trace JSON (same structure)
        trace_json = trace_to_export_dict(trace)
        trace_file = Path(tmp) / "trace.json"
        trace_file.write_text(json.dumps(trace_json, ensure_ascii=False), encoding="utf-8")

        # simulate a drifted trace with a new arg → strict test fails, no-regression passes
        drifted = json.loads(trace_file.read_text(encoding="utf-8"))
        drifted["spans"][0]["arguments"]["lang"] = "zh"

        import os
        env = dict(os.environ)
        env["TRACE_FILE"] = str(trace_file)

        # 1. matching trace → both tests pass
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_file)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, f"matching trace should pass:\n{r.stdout}\n{r.stderr}"

        # 2. drifted trace → test_no_regression still passes, strict fails
        drift_file = Path(tmp) / "drifted.json"
        drift_file.write_text(json.dumps(drifted, ensure_ascii=False), encoding="utf-8")
        env["TRACE_FILE"] = str(drift_file)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_file)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode != 0, "drifted trace should fail strict test"
        assert "test_no_regression" not in r.stdout or "1 failed" in r.stdout

        # 3. regressed trace (api_call now fails) → no_regression test fails too
        regressed = json.loads(trace_file.read_text(encoding="utf-8"))
        regressed["spans"][1]["error"] = "timeout after 30s"
        reg_file = Path(tmp) / "regressed.json"
        reg_file.write_text(json.dumps(regressed, ensure_ascii=False), encoding="utf-8")
        env["TRACE_FILE"] = str(reg_file)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_file)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode != 0, "regressed trace should fail"
        assert "REGRESSION detected" in r.stdout


def test_trace_to_export_dict():
    trace = make_trace(GOLDEN)
    d = trace_to_export_dict(trace)
    assert d["id"] == "abc123"
    assert d["agent"] == "market-bot"
    assert len(d["spans"]) == 3
    assert d["spans"][0]["tool_name"] == "web_search"
    assert d["spans"][0]["arguments"] == {"query": "上证指数"}
    # round-trip: dict can be fed to compare via a rehydrated Trace
    from src.models import Trace as T
    rehydrated = T(id=d["id"], agent=d["agent"], task=d["task"],
                   spans=[Span(**s) for s in d["spans"]])
    from src.regression import compare
    r = compare(trace_signature(trace), rehydrated)
    assert r.verdict == "match"


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
        except Exception as e:
            print(f"❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
