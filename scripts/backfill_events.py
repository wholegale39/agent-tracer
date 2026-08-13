"""One-off backfill: migrate historical webhook_events into the event stream.

Reads every webhook_events row, groups by session_id (preserving id order),
creates one trace per session (idempotent — skips sessions that already have
a trace), and replays tool calls / session end into the event stream.

Run from the repo root:
    venv/bin/python scripts/backfill_events.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Span, Trace  # noqa: E402
from src.store import TraceStore  # noqa: E402

RESULT_TRUNCATE = 4000


async def main() -> None:
    store = TraceStore()
    await store.connect()

    rows = await store._conn.execute_fetchall(
        "SELECT * FROM webhook_events ORDER BY id"
    )
    sessions: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        sid = d.get("session_id") or ""
        if not sid:
            continue
        sessions.setdefault(sid, []).append(d)

    total_events = sum(len(v) for v in sessions.values())
    print(f"webhook_events 共 {len(rows)} 条 → {len(sessions)} 个 session")

    created = skipped = 0
    span_count = 0
    for i, (sid, events) in enumerate(sessions.items()):
        # Idempotent: skip sessions already represented in traces
        existing = await store._conn.execute_fetchall(
            "SELECT id FROM traces WHERE session_id = ? LIMIT 1", (sid,)
        )
        if existing:
            skipped += 1
            continue

        trace_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await store._conn.execute(
            "INSERT INTO traces (id, agent, task, session_id, started_at) VALUES (?, ?, ?, ?, ?)",
            (trace_id, "hermes", f"session:{sid}", sid, now),
        )
        # trace.started event (batch: single commit at the end)
        seq = 1
        await store._conn.execute(
            "INSERT INTO events (trace_id, seq, ts, type, payload) VALUES (?, 1, ?, 'trace.started', ?)",
            (trace_id, now, json.dumps({"agent": "hermes", "task": f"session:{sid}", "session_id": sid}, ensure_ascii=False)),
        )
        created += 1

        span_seq = 0
        for e in events:
            ev = e.get("hook_event_name")
            try:
                payload = json.loads(e.get("payload") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if ev == "post_tool_call":
                tool_name = payload.get("tool_name") or "unknown"
                tool_input = payload.get("tool_input") or {}
                extra = payload.get("extra") or {}
                result = extra.get("result")
                if result is not None and not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                if isinstance(result, str) and len(result) > RESULT_TRUNCATE:
                    result = result[:RESULT_TRUNCATE]
                span_seq += 1
                await store._conn.execute(
                    """INSERT INTO spans (trace_id, sequence, tool_name, arguments, result, error, started_at)
                       VALUES (?, ?, ?, ?, ?, NULL, ?)""",
                    (trace_id, span_seq, tool_name,
                     json.dumps(tool_input, ensure_ascii=False), result, now),
                )
                seq += 1
                await store._conn.execute(
                    "INSERT INTO events (trace_id, seq, ts, type, payload) VALUES (?, ?, ?, 'tool.call', ?)",
                    (trace_id, seq, now, json.dumps({"tool_name": tool_name, "arguments": tool_input}, ensure_ascii=False)),
                )
                if result is not None:
                    seq += 1
                    await store._conn.execute(
                        "INSERT INTO events (trace_id, seq, ts, type, payload) VALUES (?, ?, ?, 'tool.result', ?)",
                        (trace_id, seq, now, json.dumps({"result": result}, ensure_ascii=False)),
                    )
                span_count += 1
            elif ev == "on_session_end":
                extra = payload.get("extra") or {}
                completed = bool(extra.get("completed", True))
                seq += 1
                await store._conn.execute(
                    "INSERT INTO events (trace_id, seq, ts, type, payload) VALUES (?, ?, ?, 'trace.finished', ?)",
                    (trace_id, seq, now, json.dumps(
                        {"finish_reason": "completed" if completed else "interrupted",
                         "turn_id": extra.get("turn_id", ""), "source": "backfill"},
                        ensure_ascii=False)),
                )
                await store._conn.execute(
                    "UPDATE traces SET finished_at = ? WHERE id = ?", (now, trace_id),
                )

        # Batch commit every 25 sessions (SQLite write amplification control)
        if (i + 1) % 25 == 0:
            await store._conn.commit()
            print(f"  ...{i + 1}/{len(sessions)} sessions, {span_count} spans", flush=True)

    await store._conn.commit()
    print(f"✅ 新建 trace: {created} | 跳过(已有): {skipped} | 回填 spans: {span_count}")
    print(f"   事件总数: {total_events}")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
