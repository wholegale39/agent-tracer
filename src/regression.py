"""Trace regression engine — turn traces into golden cases, detect drift/regression.

The idea: a *golden case* is a recorded trace promoted as the expected behavior
for an agent+task. Later traces of the same agent+task are compared against the
golden case's *signature* (tool sequence + argument shape + per-span status) to
answer: "did the behavior drift, and did anything start failing?"

Verdicts:
- match      — new trace is structurally identical to the golden case
- drift      — structure changed (tool order/args) but nothing new failed
- regression — a span that was ok is now failing, or the sequence is very different
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Signature extraction ────────────────────────────────────────


def _status(span: Any) -> str:
    """Normalize a span's outcome to 'ok' | 'error'."""
    err = getattr(span, "error", None)
    return "error" if err else "ok"


def _arg_keys(arguments: Optional[dict]) -> list[str]:
    """Top-level argument key set, sorted. Nested values are not descended —
    top-level key drift is enough for regression detection."""
    if not arguments:
        return []
    return sorted(str(k) for k in arguments.keys())


def span_signature(span: Any) -> dict:
    """Structural fingerprint of one span (tool name, arg keys, outcome)."""
    return {
        "tool": getattr(span, "tool_name", "?"),
        "arg_keys": _arg_keys(getattr(span, "arguments", None)),
        "status": _status(span),
    }


def trace_signature(trace: Any) -> dict:
    """Structural fingerprint of a whole trace: tool sequence + per-span shapes."""
    spans = list(getattr(trace, "spans", []))
    return {
        "agent": getattr(trace, "agent", ""),
        "task": getattr(trace, "task", ""),
        "tool_sequence": [s.tool_name for s in spans],
        "span_count": len(spans),
        "spans": [span_signature(s) for s in spans],
    }


# ── Diff / verdict ──────────────────────────────────────────────


@dataclass
class DiffItem:
    """One difference between golden and new trace."""

    kind: str          # span_added | span_removed | tool_changed | arg_keys_changed | status_flipped
    index: int         # position in the golden trace
    golden: Any = None
    new: Any = None
    message: str = ""


@dataclass
class CheckResult:
    """Full comparison result."""

    verdict: str                       # match | drift | regression
    score: float                       # 1.0 = identical, lower = more different
    diffs: list[DiffItem] = field(default_factory=list)
    summary: str = ""


def compare(golden_signature: dict, new_trace: Any) -> CheckResult:
    """Compare a golden signature against a new trace. Pure function, no I/O."""
    new_spans = list(getattr(new_trace, "spans", []))
    gold_spans = golden_signature.get("spans", [])
    gold_seq = golden_signature.get("tool_sequence", [])

    diffs: list[DiffItem] = []
    regression = False

    # Position-by-position comparison over the golden span list
    for i, g in enumerate(gold_spans):
        if i >= len(new_spans):
            diffs.append(DiffItem(
                "span_removed", i, g, None,
                f"span #{i + 1} ({g['tool']}) missing from new trace"))
            regression = True
            continue

        n = span_signature(new_spans[i])

        if g["tool"] != n["tool"]:
            diffs.append(DiffItem(
                "tool_changed", i, g, n,
                f"span #{i + 1}: tool {g['tool']} → {n['tool']}"))
        elif g["arg_keys"] != n["arg_keys"]:
            added = sorted(set(n["arg_keys"]) - set(g["arg_keys"]))
            removed = sorted(set(g["arg_keys"]) - set(n["arg_keys"]))
            diffs.append(DiffItem(
                "arg_keys_changed", i, g, n,
                f"span #{i + 1} ({g['tool']}): args {g['arg_keys']} → {n['arg_keys']}"
                f"{' +' + str(added) if added else ''}{' -' + str(removed) if removed else ''}"))

        if g["status"] == "ok" and n["status"] == "error":
            diffs.append(DiffItem(
                "status_flipped", i, g, n,
                f"span #{i + 1} ({g['tool']}): was ok, now FAILING "
                f"({getattr(new_spans[i], 'error', '')[:120]})"))
            regression = True

    # Extra spans at the end of the new trace
    for j in range(len(gold_spans), len(new_spans)):
        n = span_signature(new_spans[j])
        diffs.append(DiffItem(
            "span_added", j, None, n,
            f"new span #{j + 1} ({n['tool']}) not in golden case"))

    # Score: fraction of golden spans that still match position & shape
    matched = sum(
        1 for i, g in enumerate(gold_spans)
        if i < len(new_spans) and span_signature(new_spans[i]) == g
    )
    total = max(len(gold_spans), 1)
    score = round(matched / total, 2)

    if regression:
        verdict = "regression"
    elif diffs:
        verdict = "drift"
    else:
        verdict = "match"

    summary = _summarize(verdict, diffs, len(gold_spans), len(new_spans))
    return CheckResult(verdict=verdict, score=score, diffs=diffs, summary=summary)


def _summarize(verdict: str, diffs: list, gold_n: int, new_n: int) -> str:
    if verdict == "match":
        return f"Identical to golden case ({gold_n} spans)."
    parts = [f"{len(diffs)} difference(s) vs golden case"]
    for d in diffs[:3]:
        parts.append(d.message)
    if len(diffs) > 3:
        parts.append(f"... and {len(diffs) - 3} more")
    return "; ".join(parts)


# ── Case naming ─────────────────────────────────────────────────

def suggest_case_name(agent: str, task: str, trace_id: str) -> str:
    """Human-readable case name from agent+task, e.g. 'market-bot/收盘汇总'."""
    base = f"{agent or 'unknown'}/{task or 'untitled'}"
    return f"{base} · {trace_id[:8]}"


# ── Export: self-contained pytest regression test ──────────────
#
# Generates a standalone pytest file embedding the golden signature and a
# copy of the compare logic — no dependency on this package, so it can be
# dropped into ANY project's tests/ directory and run against future traces.

_EXPORT_TEMPLATE = '''\
"""Golden-case regression test — AUTO-GENERATED by agent-tracer (v0.3).

Case:    @@CASE_NAME@@
Agent:   @@AGENT@@
Task:    @@TASK@@
Source:  trace @@SOURCE_TRACE_ID@@

This file is self-contained: it embeds the golden signature captured when the
source trace was promoted, plus a copy of the comparison logic. Point it at a
new trace (see TRACE_FILE below) and it fails if the new run regressed.

Usage:
    export TRACE_FILE=/path/to/trace.json && pytest test_@@CASE_ID@@.py
    # or drop a trace.json next to this file — it is picked up automatically.
"""

import json
import os
from pathlib import Path

# ── Golden signature (captured at promote time) ─────────────────
GOLDEN_SIGNATURE = @@SIGNATURE@@

# Where to read the trace under test. Priority:
#   1. $TRACE_FILE env var
#   2. ./trace.json next to this test file
DEFAULT_TRACE_FILE = Path(__file__).parent / "trace.json"


def _load_trace():
    path = os.environ.get("TRACE_FILE")
    if path:
        p = Path(path)
    elif DEFAULT_TRACE_FILE.exists():
        p = DEFAULT_TRACE_FILE
    else:
        raise RuntimeError(
            "No trace to check. Set TRACE_FILE=/path/to/trace.json "
            "or place trace.json next to this test."
        )
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _status(span):
    return "error" if span.get("error") else "ok"


def _arg_keys(arguments):
    return sorted(str(k) for k in (arguments or {}).keys())


def _span_sig(span):
    return {
        "tool": span.get("tool_name", "?"),
        "arg_keys": _arg_keys(span.get("arguments")),
        "status": _status(span),
    }


def compare_trace(trace: dict) -> dict:
    """Compare a trace dict against the embedded golden signature."""
    spans = trace.get("spans", [])
    gold = GOLDEN_SIGNATURE.get("spans", [])
    diffs = []
    regression = False

    for i, g in enumerate(gold):
        if i >= len(spans):
            diffs.append(f"span #{i + 1} ({g['tool']}) missing from new trace")
            regression = True
            continue
        n = _span_sig(spans[i])
        if g["tool"] != n["tool"]:
            diffs.append(f"span #{i + 1}: tool {g['tool']} -> {n['tool']}")
        elif g["arg_keys"] != n["arg_keys"]:
            diffs.append(f"span #{i + 1} ({g['tool']}): args {g['arg_keys']} -> {n['arg_keys']}")
        if g["status"] == "ok" and n["status"] == "error":
            diffs.append(f"span #{i + 1} ({g['tool']}): was ok, now FAILING ({spans[i].get('error', '')[:120]})")
            regression = True
    for j in range(len(gold), len(spans)):
        diffs.append(f"new span #{j + 1} ({_span_sig(spans[j])['tool']}) not in golden case")

    verdict = "regression" if regression else ("drift" if diffs else "match")
    matched = sum(
        1 for i, g in enumerate(gold)
        if i < len(spans) and _span_sig(spans[i]) == g
    )
    return {
        "verdict": verdict,
        "score": round(matched / max(len(gold), 1), 2),
        "diffs": diffs,
        "new_span_count": len(spans),
        "golden_span_count": len(gold),
    }


def test_no_regression():
    """The new trace must not fail any step that succeeded in the golden case."""
    result = compare_trace(_load_trace())
    assert result["verdict"] != "regression", (
        f"REGRESSION detected (score {result['score']})\\n"
        + "\\n".join(result["diffs"])
    )


def test_structure_matches_golden():
    """Strict check: tool sequence + argument shapes identical to golden."""
    result = compare_trace(_load_trace())
    assert result["verdict"] == "match", (
        f"verdict={result['verdict']} score={result['score']}\\n"
        + "\\n".join(result["diffs"])
    )
'''


def export_pytest_case(signature: dict, case_id: str, case_name: str,
                       source_trace_id: str) -> str:
    """Render a self-contained pytest file for a golden case.

    Uses token replacement (not str.format) so the template body can contain
    arbitrary Python with braces.
    """
    code = _EXPORT_TEMPLATE
    for token, value in [
        ("@@CASE_NAME@@", case_name),
        ("@@CASE_ID@@", case_id),
        ("@@AGENT@@", signature.get("agent", "")),
        ("@@TASK@@", signature.get("task", "")),
        ("@@SOURCE_TRACE_ID@@", source_trace_id),
        ("@@SIGNATURE@@", json.dumps(signature, ensure_ascii=False, indent=2)),
    ]:
        code = code.replace(token, value)
    return code


def trace_to_export_dict(trace) -> dict:
    """Serialize a Trace object to a plain dict (for JSON export)."""
    return {
        "id": trace.id,
        "agent": trace.agent,
        "task": trace.task,
        "session_id": trace.session_id,
        "started_at": trace.started_at,
        "finished_at": trace.finished_at,
        "spans": [
            {
                "id": s.id,
                "trace_id": s.trace_id,
                "sequence": s.sequence,
                "tool_name": s.tool_name,
                "arguments": s.arguments,
                "result": s.result,
                "error": s.error,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
                "duration_ms": s.duration_ms,
            }
            for s in trace.spans
        ],
    }
