"""FastAPI service for Agent Call Tracer."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .models import ReplayRequest, Span, SpanIn, Trace, TraceIn
from . import cost
from .regression import (CheckResult, compare, export_pytest_case,
                         suggest_case_name, trace_signature,
                         trace_to_export_dict)
from .store import TraceStore
from .webhook_rx import router as hermes_webhook_router


store: Optional[TraceStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = TraceStore()
    await store.connect()
    yield
    await store.close()


app = FastAPI(title="Agent Call Tracer", version="0.4.0", lifespan=lifespan)
app.include_router(hermes_webhook_router)


# ── Traces ───────────────────────────────────────────────

@app.post("/traces")
async def start_trace(t: TraceIn):
    """Start a new trace."""
    trace = Trace(
        id=uuid.uuid4().hex[:12],
        agent=t.agent,
        task=t.task,
        session_id=t.session_id,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    await store.create_trace(trace)
    return {"trace_id": trace.id}


@app.post("/traces/{trace_id}/finish")
async def finish_trace(trace_id: str):
    """Mark a trace as finished."""
    await store.finish_trace(trace_id)
    return {"ok": True}


@app.get("/traces")
async def list_traces(limit: int = 20, agent: str = ""):
    """List recent traces."""
    return await store.list_traces(limit=limit, agent=agent)


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """Get a full trace with all spans."""
    trace = await store.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "Trace not found")
    return trace


# ── Spans ───────────────────────────────────────────────

@app.post("/traces/{trace_id}/spans")
async def add_span(trace_id: str, span_in: SpanIn):
    """Record a tool call span."""
    now = datetime.now(timezone.utc).isoformat()
    span = Span(
        tool_name=span_in.tool_name,
        arguments=span_in.arguments,
        result=span_in.result,
        error=span_in.error,
        started_at=span_in.started_at or now,
        finished_at=span_in.finished_at,
        duration_ms=span_in.duration_ms,
        model=span_in.model,
        input_tokens=span_in.input_tokens,
        output_tokens=span_in.output_tokens,
    )
    span_id = await store.add_span(trace_id, span)
    return {"span_id": span_id, "trace_id": trace_id}


@app.get("/spans")
async def list_spans(
    trace_id: str = "",
    tool_name: str = "",
    limit: int = 50,
):
    """List spans, filtered by trace or tool."""
    return await store.list_spans(trace_id=trace_id, tool_name=tool_name, limit=limit)


@app.get("/spans/errors")
async def list_errors(limit: int = 20):
    """List spans with errors."""
    return await store.list_errors(limit=limit)


@app.get("/spans/{span_id}")
async def get_span(span_id: int):
    """Get a specific span."""
    span = await store.get_span(span_id)
    if not span:
        raise HTTPException(404, "Span not found")
    return span


# ── Replay ──────────────────────────────────────────────

@app.post("/spans/{span_id}/replay")
async def replay_span(span_id: int, req: ReplayRequest):
    """Replay a recorded span with optional argument overrides."""
    span = await store.get_span(span_id)
    if not span:
        raise HTTPException(404, "Span not found")

    # Merge original arguments with overrides
    merged_args = dict(span.arguments)
    merged_args.update(req.arguments_override)

    return {
        "original_span_id": span_id,
        "original_tool": span.tool_name,
        "original_arguments": span.arguments,
        "merged_arguments": merged_args,
        "replay_instructions": _replay_instructions(span.tool_name, merged_args),
    }


def _replay_instructions(tool_name: str, args: dict) -> str:
    """Generate instructions for how to replay this tool call."""

    # For HTTP-based tools, generate a curl command
    url = args.get("url", "")
    if url:
        method = args.get("method", "GET")
        body = args.get("body", args.get("data", ""))
        cmd = f"curl -s -X {method} '{url}'"
        if body:
            cmd += f" -H 'Content-Type: application/json' -d '{json.dumps(body) if isinstance(body, dict) else body}'"
        return cmd

    # For terminal commands
    command = args.get("command", "")
    if command:
        return command

    # For search queries
    query = args.get("query", args.get("question", ""))
    if query:
        return f"Search/query: {query}"

    return json.dumps(args, indent=2)


# ── Regression cases ─────────────────────────────────────────

@app.post("/traces/{trace_id}/promote")
async def promote_trace(trace_id: str, task: str = ""):
    """Promote a completed trace into a golden regression case."""
    trace = await store.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "Trace not found")
    if not trace.spans:
        raise HTTPException(400, "Trace has no spans — nothing to promote")

    import uuid
    case_id = uuid.uuid4().hex[:12]
    sig = trace_signature(trace)
    name = suggest_case_name(trace.agent, task or trace.task, trace_id)
    await store.create_case(
        case_id, name, sig["agent"], task or trace.task,
        trace_id, sig,
    )
    return {
        "case_id": case_id,
        "name": name,
        "agent": sig["agent"],
        "task": task or trace.task,
        "source_trace_id": trace_id,
        "spans": sig["span_count"],
        "signature": sig,
    }


@app.get("/cases")
async def list_cases(agent: str = "", limit: int = 50):
    """List golden regression cases."""
    return await store.list_cases(agent=agent, limit=limit)


@app.get("/cases/{case_id}")
async def get_case(case_id: str):
    """Get a regression case with its signature."""
    case = await store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    import json
    case = dict(case)
    case["signature"] = json.loads(case["signature"])
    return case


@app.post("/cases/{case_id}/check")
async def check_case(case_id: str, trace_id: str = ""):
    """Compare a golden case against a new trace.

    Pass ?trace_id=xxx to check a specific trace, or omit to check the
    agent's most recent trace automatically.
    """
    case = await store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")

    import json
    sig = json.loads(case["signature"])

    if trace_id:
        trace = await store.get_trace(trace_id)
        if not trace:
            raise HTTPException(404, "Trace not found")
    else:
        # Auto: find most recent trace for the case's agent+task
        candidates = await store.list_traces(limit=50, agent=sig.get("agent", ""))
        if not candidates:
            raise HTTPException(404, "No traces found for this agent")
        target = candidates[0]
        if sig.get("task") and target["task"] != sig["task"]:
            # look for a task match first
            for c in candidates:
                if c["task"] == sig["task"]:
                    target = c
                    break
        trace = await store.get_trace(target["id"])
        if not trace:
            raise HTTPException(404, "Trace not found")

    result = compare(sig, trace)
    await store.update_case_result(case_id, result.verdict, result.score)

    return {
        "case_id": case_id,
        "name": case["name"],
        "checked_trace_id": trace.id,
        "checked_agent": trace.agent,
        "checked_task": trace.task,
        "verdict": result.verdict,
        "score": result.score,
        "summary": result.summary,
        "diffs": [d.__dict__ for d in result.diffs],
        "golden_span_count": len(sig.get("spans", [])),
        "new_span_count": len(trace.spans),
    }


@app.post("/traces/{trace_id}/check")
async def check_trace_against_golden(trace_id: str):
    """Check a trace against the golden case for its agent (if one exists)."""
    trace = await store.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "Trace not found")

    case = await store.find_case_for_agent(trace.agent, trace.task)
    if not case:
        return {
            "found": False,
            "message": f"No golden case for agent '{trace.agent}' yet — "
                       f"promote one with POST /traces/{trace_id}/promote",
        }

    import json
    sig = json.loads(case["signature"])
    result = compare(sig, trace)
    await store.update_case_result(case["id"], result.verdict, result.score)

    return {
        "found": True,
        "case_id": case["id"],
        "case_name": case["name"],
        "verdict": result.verdict,
        "score": result.score,
        "summary": result.summary,
        "diffs": [d.__dict__ for d in result.diffs],
        "golden_span_count": len(sig.get("spans", [])),
        "new_span_count": len(trace.spans),
    }


# ── Export & drift report ──────────────────────────────────────

@app.get("/traces/{trace_id}/export")
async def export_trace(trace_id: str):
    """Export a trace as plain JSON (offline analysis / feed to pytest case)."""
    trace = await store.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "Trace not found")
    return trace_to_export_dict(trace)


@app.get("/cases/{case_id}/export")
async def export_case_pytest(case_id: str):
    """Export a golden case as a self-contained pytest regression test file."""
    case = await store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")

    import json
    sig = json.loads(case["signature"])
    code = export_pytest_case(
        signature=sig,
        case_id=case["id"],
        case_name=case["name"],
        source_trace_id=case["source_trace_id"],
    )
    filename = f"test_golden_{case['id']}.py"
    return PlainTextResponse(
        code,
        media_type="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/agents/{agent}/drift-report")
async def drift_report(agent: str, limit: int = 20):
    """Batch-compare an agent's recent traces against its golden case.

    Returns per-trace verdicts plus aggregate stats: how many matched,
    drifted, regressed, and the first regression time if any.
    """
    case = await store.find_case_for_agent(agent)
    if not case:
        return {
            "agent": agent,
            "golden_case": None,
            "message": f"No golden case for agent '{agent}' yet — promote a trace first",
        }

    import json
    sig = json.loads(case["signature"])
    traces = await store.list_traces(limit=limit, agent=agent)

    results = []
    regressions = []
    for t in traces:
        trace = await store.get_trace(t["id"])
        if not trace:
            continue
        r = compare(sig, trace)
        results.append({
            "trace_id": trace.id,
            "started_at": trace.started_at,
            "verdict": r.verdict,
            "score": r.score,
            "diff_count": len(r.diffs),
        })
        if r.verdict == "regression":
            regressions.append(trace.started_at)

    total = len(results)
    counts = {"match": 0, "drift": 0, "regression": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    return {
        "agent": agent,
        "golden_case": {
            "id": case["id"],
            "name": case["name"],
            "source_trace_id": case["source_trace_id"],
            "span_count": len(sig.get("spans", [])),
        },
        "checked_traces": total,
        "counts": counts,
        "regression_rate": round(counts["regression"] / total, 2) if total else 0,
        "first_regression_at": regressions[-1] if regressions else None,
        "results": results,
    }


# ── Cost & error attribution (v0.4) ────────────────────────────

@app.get("/cost/traces/{trace_id}")
async def trace_cost_report(trace_id: str):
    """Cost breakdown for one trace (per tool / per model)."""
    trace = await store.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "Trace not found")
    return cost.trace_cost(trace)


@app.get("/cost/summary")
async def cost_summary(agent: str = "", limit: int = 50, days: Optional[float] = None):
    """Aggregate cost across recent traces.

    Breakdowns: per tool, per model, per agent. Optional agent filter
    and time window (days) to scope the report.
    """
    traces = await store.list_traces(limit=limit, agent=agent)
    loaded = []
    for t in traces:
        trace = await store.get_trace(t["id"])
        if trace:
            loaded.append(trace)

    if days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        loaded = [t for t in loaded
                  if _ts(t.started_at, 0) >= cutoff]

    totals = {"traces": 0, "spans": 0, "input_tokens": 0, "output_tokens": 0,
              "total_tokens": 0, "input_cost": 0.0, "output_cost": 0.0,
              "total_cost": 0.0}
    per_tool: dict[str, dict] = {}
    per_model: dict[str, dict] = {}
    per_agent: dict[str, dict] = {}

    for t in loaded:
        c = cost.trace_cost(t)
        a = c["totals"]
        totals["traces"] += 1
        totals["spans"] += a["spans"]
        totals["input_tokens"] += a["input_tokens"]
        totals["output_tokens"] += a["output_tokens"]
        totals["total_tokens"] += a["total_tokens"]
        totals["input_cost"] = round(totals["input_cost"] + a["input_cost"], 6)
        totals["output_cost"] = round(totals["output_cost"] + a["output_cost"], 6)
        totals["total_cost"] = round(totals["total_cost"] + a["total_cost"], 6)

        for tool, d in c["per_tool"].items():
            bt = per_tool.setdefault(tool, {"calls": 0, "total_tokens": 0, "total_cost": 0.0})
            bt["calls"] += d["calls"]
            bt["total_tokens"] += d["total_tokens"]
            bt["total_cost"] = round(bt["total_cost"] + d["total_cost"], 6)
        for model, d in c["per_model"].items():
            bm = per_model.setdefault(model, {"calls": 0, "total_tokens": 0, "total_cost": 0.0})
            bm["calls"] += d["calls"]
            bm["total_tokens"] += d["total_tokens"]
            bm["total_cost"] = round(bm["total_cost"] + d["total_cost"], 6)
        ba = per_agent.setdefault(t.agent, {"traces": 0, "total_cost": 0.0, "total_tokens": 0})
        ba["traces"] += 1
        ba["total_tokens"] += a["total_tokens"]
        ba["total_cost"] = round(ba["total_cost"] + a["total_cost"], 6)

    _rank = lambda d: dict(sorted(d.items(), key=lambda kv: kv[1]["total_cost"], reverse=True))
    return {
        "scoped_traces": totals["traces"],
        "totals": totals,
        "per_tool": _rank(per_tool),
        "per_model": _rank(per_model),
        "per_agent": _rank(per_agent),
        "filters": {"agent": agent, "limit": limit, "days": days},
    }


@app.get("/errors/aggregate")
async def error_aggregate(limit: int = 500):
    """Multi-session error attribution.

    Groups error spans across traces by normalized fingerprint, sorted by
    frequency — surfaces recurring root causes instead of one-off failures.
    """
    spans = await store.list_errors(limit=limit)
    return cost.aggregate_errors(spans)


def _ts(iso: Optional[str], default: float) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return default


# ── Health ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}
