"""Hermes outbound webhook receiver for agent-tracer.

Receives signed lifecycle events pushed by Hermes hooks.outbound
(config.yaml) and stores them in a dedicated table. Does not touch the
traces/spans/cases logic — this is an ingest side-channel.

Verification: HMAC-SHA256 (GitHub-style) when HERMES_OUTBOUND_WEBHOOK_SECRET
is set in the environment. If no secret is configured, events are stored
unverified (visible as verified=0).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request

router = APIRouter(prefix="/hermes-events", tags=["hermes-webhook"])


def _secret() -> bytes | None:
    s = os.environ.get("HERMES_OUTBOUND_WEBHOOK_SECRET", "").strip()
    return s.encode() if s else None


async def _ensure_table() -> None:
    from .api import store
    if store is None:
        raise RuntimeError("store not initialized")
    await store._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL,
            hook_event_name TEXT NOT NULL,
            session_id TEXT DEFAULT '',
            tool_name TEXT,
            cwd TEXT DEFAULT '',
            payload TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wev_event ON webhook_events(hook_event_name);
        CREATE INDEX IF NOT EXISTS idx_wev_delivery ON webhook_events(delivery_id);
        """
    )
    await store._conn.commit()


@router.post("")
@router.post("/")
async def receive_event(
    request: Request,
    x_hermes_signature_256: str | None = Header(default=None),
    x_hermes_event: str | None = Header(default=None),
    x_hermes_delivery: str | None = Header(default=None),
):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw.decode(errors="replace")}

    # HMAC verification (GitHub-style) — best-effort, never blocks ingest
    verified = 0
    secret = _secret()
    if secret is not None and x_hermes_signature_256:
        try:
            _, _, hexdigest = x_hermes_signature_256.partition("sha256=")
            expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
            verified = 1 if hmac.compare_digest(hexdigest.strip(), expected) else 0
        except Exception:
            verified = 0

    event_name = payload.get("hook_event_name") or x_hermes_event or "unknown"
    delivery_id = payload.get("delivery_id") or x_hermes_delivery or uuid.uuid4().hex

    await _ensure_table()
    from .api import store
    await store._conn.execute(
        """
        INSERT INTO webhook_events
            (delivery_id, hook_event_name, session_id, tool_name, cwd, payload, verified, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            event_name,
            payload.get("session_id", "") or "",
            payload.get("tool_name"),
            payload.get("cwd", "") or "",
            json.dumps(payload, ensure_ascii=False),
            verified,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    # v0.5: sync Hermes lifecycle events into the append-only event stream.
    # Same transaction as the webhook_events insert (committed together below).
    try:
        await _sync_event_stream(payload, verified)
    except Exception:
        # Event-stream sync is best-effort; never let it break webhook ingest.
        pass
    await store._conn.commit()

    return {"ok": True, "delivery_id": delivery_id, "event": event_name, "verified": bool(verified)}


# ── v0.5: webhook → event-stream sync ──────────────────────────
# Hermes hooks.outbound emits: on_session_start / post_tool_call / on_session_end.
# These are mapped onto the append-only events stream so a trace captures
# the full run: session start, every tool call (args + result), and completion.

# session_id -> trace_id in-memory cache (webhook volume is high; avoids a
# DB query per event). Invalidated lazily — a missing trace falls back to a
# lookup, then to auto-create.
_session_trace_cache: dict[str, str] = {}

_RESULT_TRUNCATE = 4000  # keep the event stream lean; full result stays in webhook_events


def _session_id_of(payload: dict) -> str:
    return str(payload.get("session_id") or "")


async def _get_or_create_trace(session_id: str) -> str | None:
    """Return trace_id for a Hermes session, creating one on first sight."""
    from .api import store
    cached = _session_trace_cache.get(session_id)
    if cached:
        return cached
    # Look for an open trace for this session
    rows = await store._conn.execute_fetchall(
        "SELECT id FROM traces WHERE session_id = ? AND finished_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    )
    if rows:
        trace_id = rows[0]["id"]
    else:
        import uuid as _uuid
        from .models import Trace
        trace = Trace(
            id=_uuid.uuid4().hex[:12],
            agent="hermes",
            task=f"session:{session_id}",
            session_id=session_id,
        )
        trace_id = await store.create_trace(trace)
    _session_trace_cache[session_id] = trace_id
    # Keep the cache bounded (sessions churn; 2k is plenty)
    if len(_session_trace_cache) > 2000:
        _session_trace_cache.clear()
    return trace_id


async def _sync_event_stream(payload: dict, verified: int) -> None:
    """Map a Hermes webhook payload onto the event stream (best-effort)."""
    from .api import store
    event = payload.get("hook_event_name") or ""
    session_id = _session_id_of(payload)

    if not session_id:
        return

    if event == "on_session_start":
        trace_id = await _get_or_create_trace(session_id)
        # trace.started already appended by create_trace; only add when reusing
        return

    if event == "post_tool_call":
        tool_name = payload.get("tool_name") or "unknown"
        tool_input = payload.get("tool_input") or {}
        extra = payload.get("extra") or {}
        result = extra.get("result")
        trace_id = await _get_or_create_trace(session_id)
        if not trace_id:
            return
        # add_span writes BOTH the spans projection and the tool.call/result
        # events, so regression/cost/drift keep working on webhook-fed traces.
        from .models import Span
        if result is not None and not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        if isinstance(result, str) and len(result) > _RESULT_TRUNCATE:
            result = result[:_RESULT_TRUNCATE]
        await store.add_span(trace_id, Span(
            tool_name=tool_name,
            arguments=tool_input,
            result=result,
        ))
        return

    if event == "on_session_end":
        trace_id = _session_trace_cache.get(session_id)
        if not trace_id:
            rows = await store._conn.execute_fetchall(
                "SELECT id FROM traces WHERE session_id = ? ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            )
            trace_id = rows[0]["id"] if rows else None
        if not trace_id:
            return
        extra = payload.get("extra") or {}
        completed = bool(extra.get("completed", True))
        await store.append_event(trace_id, "trace.finished", {
            "finish_reason": "completed" if completed else "interrupted",
            "turn_id": extra.get("turn_id", ""),
            "source": "webhook",
        })
        # Close the trace without re-appending trace.finished (finish_trace
        # appends its own event — avoid the duplicate).
        now = datetime.now(timezone.utc).isoformat()
        await store._conn.execute(
            "UPDATE traces SET finished_at = ? WHERE id = ?", (now, trace_id)
        )
        _session_trace_cache.pop(session_id, None)
        return


@router.get("/stats")
async def event_stats():
    """Quick counts for verification: total, per-event breakdown, verified rate."""
    await _ensure_table()
    from .api import store
    cur = await store._conn.execute("SELECT COUNT(*) FROM webhook_events")
    total = (await cur.fetchone())[0]
    cur = await store._conn.execute(
        "SELECT hook_event_name, COUNT(*) FROM webhook_events GROUP BY hook_event_name ORDER BY 2 DESC"
    )
    per_event = {row[0]: row[1] for row in await cur.fetchall()}
    cur = await store._conn.execute("SELECT COUNT(*) FROM webhook_events WHERE verified = 1")
    verified = (await cur.fetchone())[0]
    return {"total": total, "verified": verified, "per_event": per_event}


@router.get("/recent")
async def recent_events(limit: int = 10):
    await _ensure_table()
    from .api import store
    cur = await store._conn.execute(
        "SELECT delivery_id, hook_event_name, session_id, tool_name, verified, received_at "
        "FROM webhook_events ORDER BY id DESC LIMIT ?",
        (min(limit, 100),),
    )
    rows = await cur.fetchall()
    return [
        {
            "delivery_id": r[0],
            "event": r[1],
            "session_id": r[2],
            "tool_name": r[3],
            "verified": r[4],
            "received_at": r[5],
        }
        for r in rows
    ]
