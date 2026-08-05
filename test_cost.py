"""Unit tests for the v0.4 cost & error-attribution engine (pure logic, no server)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.cost import (DEFAULT_PRICE, MODEL_PRICING, aggregate_errors,
                      model_price, span_cost, trace_cost)
from src.models import Span, Trace


def make_span(tool, model=None, inp=0, out=0, error=None, trace_id="t"):
    return Span(id=1, trace_id=trace_id, sequence=1, tool_name=tool,
                arguments={}, result=None if error else "ok", error=error,
                duration_ms=100, model=model, input_tokens=inp, output_tokens=out)


# ── model_price ─────────────────────────────────────────

def test_model_price_exact_and_case_insensitive():
    assert model_price("deepseek-v4-flash") == MODEL_PRICING["deepseek-v4-flash"]
    assert model_price("GPT-4o") == MODEL_PRICING["gpt-4o"]  # case-insensitive


def test_model_price_prefix_match():
    # version suffixes beyond the table fall back to the base entry
    assert model_price("deepseek-v4-flash")["input"] == 0.28
    assert model_price("deepseek-chat-1.5")["input"] == 0.28


def test_model_price_unknown_and_none():
    p = model_price("brand-new-model-9000")
    assert p == DEFAULT_PRICE
    assert model_price(None) == DEFAULT_PRICE
    assert model_price("") == DEFAULT_PRICE


# ── span_cost ───────────────────────────────────────────

def test_span_cost_math():
    # 1M input @ $0.28 + 500k output @ $0.42
    c = span_cost(make_span("terminal", model="deepseek-v4-flash",
                            inp=1_000_000, out=500_000))
    assert c["input_cost"] == 0.28
    assert c["output_cost"] == 0.21
    assert c["total_cost"] == round(0.49, 6)
    assert c["total_tokens"] == 1_500_000


def test_span_cost_no_tokens_is_zero():
    c = span_cost(make_span("web_search", model="gpt-4o"))
    assert c["total_tokens"] == 0
    assert c["total_cost"] == 0.0
    assert c["model"] == "gpt-4o"


def test_span_cost_unknown_model_uses_default():
    c = span_cost(make_span("tool_a", model="mystery-model", inp=1_000_000, out=1_000_000))
    assert c["total_cost"] == round(0.5 + 1.5, 6)  # DEFAULT_PRICE


# ── trace_cost ──────────────────────────────────────────

def test_trace_cost_aggregates_tools_and_models():
    trace = Trace(id="t1", agent="bot", task="job", spans=[
        make_span("web_search", model="deepseek-v4-flash", inp=500_000, out=100_000),
        make_span("web_search", model="deepseek-v4-flash", inp=500_000, out=100_000),
        make_span("terminal", model="gpt-4o-mini", inp=1_000_000, out=200_000),
    ])
    r = trace_cost(trace)
    t = r["totals"]
    assert t["spans"] == 3
    assert t["total_tokens"] == 2_400_000
    # 1.2M in + 0.4M out across all spans
    assert t["input_tokens"] == 2_000_000
    assert t["output_tokens"] == 400_000
    # deepseek: 1M in @0.28 + 0.2M out @0.42 = 0.28 + 0.084
    # gpt-4o-mini: 1M in @0.15 + 0.2M out @0.60 = 0.15 + 0.12
    assert round(t["total_cost"], 6) == round(0.28 + 0.084 + 0.15 + 0.12, 6)
    assert r["per_tool"]["web_search"]["calls"] == 2
    assert r["per_tool"]["terminal"]["calls"] == 1
    assert r["per_model"]["deepseek-v4-flash"]["total_tokens"] == 1_200_000
    assert r["per_model"]["gpt-4o-mini"]["calls"] == 1
    assert r["trace_id"] == "t1"


# ── aggregate_errors ────────────────────────────────────

def test_error_fingerprint_groups_variable_parts():
    spans = [
        make_span("api_call", error="timeout after 30s calling https://x/1", trace_id="a"),
        make_span("api_call", error="timeout after 60s calling https://x/1", trace_id="b"),
        make_span("api_call", error="HTTP 404: bucket 42 not found", trace_id="c"),
        make_span("api_call", error="HTTP 404: bucket 99 not found", trace_id="d"),
        make_span("api_call", error="timeout after 30s calling https://x/1", trace_id="e"),
    ]
    r = aggregate_errors(spans)
    assert r["total_error_spans"] == 5
    assert r["distinct_fingerprints"] == 2
    top = r["buckets"][0]
    assert top["count"] == 3  # the timeout bucket is most frequent
    assert "timeout after N calling" in top["fingerprint"]
    assert sorted(top["trace_ids"]) == ["a", "b", "e"]


def test_error_fingerprint_strips_hex_and_quotes():
    spans = [
        make_span("run", error="panic at 0xdeadbeef: 'key missing'", trace_id="a"),
        make_span("run", error="panic at 0x12345678: 'key missing'", trace_id="b"),
    ]
    r = aggregate_errors(spans)
    assert r["distinct_fingerprints"] == 1
    assert r["buckets"][0]["count"] == 2


def test_aggregate_errors_ignores_success_spans():
    spans = [make_span("web_search", error=None), make_span("run", error="boom")]
    r = aggregate_errors(spans)
    assert r["total_error_spans"] == 1
    assert r["buckets"][0]["sample_error"] == "boom"


# ── store: token fields persist + old-DB migration ──────

def test_store_persists_token_fields_and_migrates_old_db(tmp_path):
    import aiosqlite

    from src.store import TraceStore

    async def run():
        # 1. Simulate an OLD database (no cost columns) and verify migration
        db = tmp_path / "old.db"
        async with aiosqlite.connect(str(db)) as conn:
            await conn.executescript("""
                CREATE TABLE traces (id TEXT PRIMARY KEY, agent TEXT, task TEXT,
                    session_id TEXT, started_at TEXT, finished_at TEXT, span_count INTEGER DEFAULT 0);
                CREATE TABLE spans (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT, sequence INTEGER, tool_name TEXT, arguments TEXT,
                    result TEXT, error TEXT, started_at TEXT, finished_at TEXT, duration_ms REAL);
            """)
            await conn.commit()

        store = TraceStore(str(db))
        await store.connect()
        # migration should have added the 3 columns
        async with aiosqlite.connect(str(db)) as conn:
            cols = [r[1] for r in (await conn.execute_fetchall("PRAGMA table_info(spans)"))]
        assert {"model", "input_tokens", "output_tokens"} <= set(cols)

        # 2. Write a span with token data and read it back
        trace = Trace(id="t1", agent="bot", task="job")
        await store.create_trace(trace)
        span = Span(trace_id="t1", sequence=1, tool_name="terminal", arguments={},
                    model="deepseek-v4-flash", input_tokens=100, output_tokens=50)
        await store.add_span("t1", span)
        got = await store.get_span(1)
        assert got.model == "deepseek-v4-flash"
        assert got.input_tokens == 100
        assert got.output_tokens == 50
        await store.close()

    asyncio.run(run())
