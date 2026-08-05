"""Cost & root-cause analysis for agent-tracer (v0.4).

- cost.py computes per-trace / per-tool / per-model cost from span token data.
- Error aggregation groups failures across traces to surface recurring root causes.

Pricing: tokens are billed per 1M tokens (input/output). Prices in USD per 1M.
Unknown models fall back to a default estimate; set price_unknown to adjust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ── Model pricing (USD per 1M tokens) ─────────────────────────
# Common models as of 2026-08. Prices are approximate list prices.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # DeepSeek
    "deepseek-v4-flash": {"input": 0.28, "output": 0.42},
    "deepseek-v4": {"input": 0.56, "output": 1.68},
    "deepseek-chat": {"input": 0.28, "output": 0.42},
    "deepseek-reasoner": {"input": 0.56, "output": 1.68},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    # Anthropic
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-haiku-3.5": {"input": 0.80, "output": 4.00},
    # Google
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    # Local / free
    "qwen2.5:7b": {"input": 0.0, "output": 0.0},   # local, free
    "llama3.2:3b": {"input": 0.0, "output": 0.0},
}

DEFAULT_PRICE: dict[str, float] = {"input": 0.50, "output": 1.50}  # unknown model estimate
UNKNOWN_LABEL = "unknown"


def model_price(model: Optional[str]) -> dict[str, float]:
    """Look up pricing for a model name; unknown models get the default estimate."""
    if not model:
        return dict(DEFAULT_PRICE)
    # normalize: lowercase, strip version suffixes that aren't in the table
    key = model.lower().strip()
    if key in MODEL_PRICING:
        return dict(MODEL_PRICING[key])
    # prefix match — try longest known name first so "gpt-4o-mini" doesn't
    # get swallowed by the shorter "gpt-4o" entry
    for known in sorted(MODEL_PRICING, key=len, reverse=True):
        if key.startswith(known):
            return dict(MODEL_PRICING[known])
    return dict(DEFAULT_PRICE)


def span_cost(span: Any) -> dict:
    """Compute cost of one span from its token usage."""
    price = model_price(getattr(span, "model", None))
    inp = getattr(span, "input_tokens", None) or 0
    out = getattr(span, "output_tokens", None) or 0
    return {
        "model": getattr(span, "model", None) or UNKNOWN_LABEL,
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "input_cost": round(inp / 1_000_000 * price["input"], 6),
        "output_cost": round(out / 1_000_000 * price["output"], 6),
        "total_cost": round(inp / 1_000_000 * price["input"]
                            + out / 1_000_000 * price["output"], 6),
    }


def trace_cost(trace: Any) -> dict:
    """Aggregate cost across all spans in a trace."""
    spans = list(getattr(trace, "spans", []))
    per_tool: dict[str, dict] = {}
    per_model: dict[str, dict] = {}
    totals = {"spans": len(spans), "input_tokens": 0, "output_tokens": 0,
              "total_tokens": 0, "input_cost": 0.0, "output_cost": 0.0,
              "total_cost": 0.0}

    for s in spans:
        c = span_cost(s)
        totals["input_tokens"] += c["input_tokens"]
        totals["output_tokens"] += c["output_tokens"]
        totals["total_tokens"] += c["total_tokens"]
        totals["input_cost"] = round(totals["input_cost"] + c["input_cost"], 6)
        totals["output_cost"] = round(totals["output_cost"] + c["output_cost"], 6)
        totals["total_cost"] = round(totals["total_cost"] + c["total_cost"], 6)

        tool = s.tool_name
        bt = per_tool.setdefault(tool, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                        "total_tokens": 0, "total_cost": 0.0})
        bt["calls"] += 1
        bt["input_tokens"] += c["input_tokens"]
        bt["output_tokens"] += c["output_tokens"]
        bt["total_tokens"] += c["total_tokens"]
        bt["total_cost"] = round(bt["total_cost"] + c["total_cost"], 6)

        model = c["model"]
        bm = per_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                          "total_tokens": 0, "total_cost": 0.0})
        bm["calls"] += 1
        bm["input_tokens"] += c["input_tokens"]
        bm["output_tokens"] += c["output_tokens"]
        bm["total_tokens"] += c["total_tokens"]
        bm["total_cost"] = round(bm["total_cost"] + c["total_cost"], 6)

    return {
        "trace_id": getattr(trace, "id", ""),
        "agent": getattr(trace, "agent", ""),
        "task": getattr(trace, "task", ""),
        "totals": totals,
        "per_tool": per_tool,
        "per_model": per_model,
    }


# ── Error / root-cause aggregation ────────────────────────────

def _error_fingerprint(error: str) -> str:
    """Normalize an error message into a fingerprint.

    Strips variable parts (ids, numbers, paths) so similar errors group together:
    'timeout after 30s' and 'timeout after 60s' → same bucket.
    """
    import re
    e = (error or "").strip()
    e = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x..", e)
    # numbers with optional unit suffix (30, 30s, 500ms, 1.5x) are variable parts
    e = re.sub(r"\b\d+(?:\.\d+)?[a-zA-Z%]*\b", "N", e)
    e = re.sub(r"['\"][^'\"]*['\"]", '".."', e)
    return e[:120]


def aggregate_errors(spans: list[Any]) -> dict:
    """Group error spans across traces into recurring root-cause buckets.

    Returns buckets sorted by occurrence count, each with sample traces.
    """
    buckets: dict[str, dict] = {}
    for s in spans:
        err = getattr(s, "error", None)
        if not err:
            continue
        fp = _error_fingerprint(err)
        b = buckets.setdefault(fp, {
            "fingerprint": fp,
            "count": 0,
            "tool": s.tool_name,
            "sample_error": err[:200],
            "trace_ids": [],
        })
        b["count"] += 1
        tid = getattr(s, "trace_id", "")
        if tid and tid not in b["trace_ids"]:
            b["trace_ids"].append(tid)

    ranked = sorted(buckets.values(), key=lambda b: b["count"], reverse=True)
    return {
        "buckets": ranked,
        "total_error_spans": sum(b["count"] for b in ranked),
        "distinct_fingerprints": len(ranked),
    }
