"""FastAPI service for Agent Call Tracer."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from .models import ReplayRequest, Span, SpanIn, Trace, TraceIn
from .regression import CheckResult, compare, suggest_case_name, trace_signature
from .store import TraceStore


store: Optional[TraceStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = TraceStore()
    await store.connect()
    yield
    await store.close()


app = FastAPI(title="Agent Call Tracer", version="0.1.0", lifespan=lifespan)


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


# ── Health ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}
