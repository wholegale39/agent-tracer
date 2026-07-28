"""SQLite storage for traces and spans."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from loguru import logger

from .models import Span, Trace


class TraceStore:

    def __init__(self, db_path: str = "data/tracer.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._init_db()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _init_db(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL DEFAULT 'unknown',
                task TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                span_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS spans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT DEFAULT '{}',
                result TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_ms REAL,
                FOREIGN KEY (trace_id) REFERENCES traces(id)
            );
            CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
            CREATE INDEX IF NOT EXISTS idx_spans_tool ON spans(tool_name);
            CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent);
        """)
        await self._conn.commit()

    # ── Traces ────────────────────────────────────────────

    async def create_trace(self, trace: Trace) -> str:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO traces (id, agent, task, session_id, started_at) VALUES (?, ?, ?, ?, ?)",
            (trace.id, trace.agent, trace.task, trace.session_id, now)
        )
        await self._conn.commit()
        return trace.id

    async def finish_trace(self, trace_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE traces SET finished_at = ? WHERE id = ?", (now, trace_id)
        )
        await self._conn.commit()

    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        row = await self._conn.execute_fetchall(
            "SELECT * FROM traces WHERE id = ?", (trace_id,)
        )
        if not row:
            return None
        trace = Trace(**dict(row[0]))
        spans = await self._conn.execute_fetchall(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY sequence", (trace_id,)
        )
        trace.spans = [Span.from_db_row(s) for s in spans]
        trace.span_count = len(trace.spans)
        return trace

    async def list_traces(self, limit: int = 20, agent: str = "") -> list[dict]:
        if agent:
            rows = await self._conn.execute_fetchall(
                "SELECT * FROM traces WHERE agent = ? ORDER BY started_at DESC LIMIT ?",
                (agent, limit)
            )
        else:
            rows = await self._conn.execute_fetchall(
                "SELECT * FROM traces ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # ── Spans ─────────────────────────────────────────────

    async def add_span(self, trace_id: str, span: Span) -> int:
        import json
        now = datetime.now(timezone.utc).isoformat()

        # Get next sequence number
        row = await self._conn.execute_fetchall(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM spans WHERE trace_id = ?",
            (trace_id,)
        )
        seq = row[0][0] if row else 1

        await self._conn.execute(
            """INSERT INTO spans (trace_id, sequence, tool_name, arguments, result, error,
               started_at, finished_at, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, seq, span.tool_name, json.dumps(span.arguments),
             span.result, span.error,
             span.started_at or now, span.finished_at, span.duration_ms)
        )
        await self._conn.execute(
            "UPDATE traces SET span_count = span_count + 1 WHERE id = ?", (trace_id,)
        )
        await self._conn.commit()

        # Return the auto-generated id
        row = await self._conn.execute_fetchall(
            "SELECT MAX(id) as id FROM spans WHERE trace_id = ?", (trace_id,)
        )
        return row[0]["id"] if row else 0

    async def get_span(self, span_id: int) -> Optional[Span]:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM spans WHERE id = ?", (span_id,)
        )
        if rows:
            return Span.from_db_row(rows[0])
        return None

    async def list_spans(self, trace_id: str = "", tool_name: str = "", limit: int = 50) -> list[Span]:
        if trace_id:
            rows = await self._conn.execute_fetchall(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY sequence LIMIT ?",
                (trace_id, limit)
            )
        elif tool_name:
            rows = await self._conn.execute_fetchall(
                "SELECT * FROM spans WHERE tool_name = ? ORDER BY id DESC LIMIT ?",
                (tool_name, limit)
            )
        else:
            rows = await self._conn.execute_fetchall(
                "SELECT * FROM spans ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [Span.from_db_row(r) for r in rows]

    async def list_errors(self, limit: int = 20) -> list[Span]:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM spans WHERE error IS NOT NULL AND error != '' ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return [Span.from_db_row(r) for r in rows]
