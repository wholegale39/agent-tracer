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
    await store._conn.commit()

    return {"ok": True, "delivery_id": delivery_id, "event": event_name, "verified": bool(verified)}


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
