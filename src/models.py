"""Agent Call Tracer — records tool calls and supports replay.

Data model:
- Trace: one agent run/session containing multiple spans
- Span: one tool call with full input/output/timing
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class SpanIn(BaseModel):
    """Record a tool call span."""
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None  # ISO timestamp
    finished_at: Optional[str] = None
    duration_ms: Optional[float] = None
    # Cost tracking (v0.4)
    model: Optional[str] = None       # model used for this call (e.g. deepseek-v4-flash)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class Span(SpanIn):
    """A recorded tool call with metadata."""
    id: int = 0
    trace_id: str = ""
    sequence: int = 0

    @classmethod
    def from_db_row(cls, row: dict) -> "Span":
        """Create Span from a DB row dict, handling JSON string fields."""
        import json
        d = dict(row)
        if isinstance(d.get("arguments"), str):
            try:
                d["arguments"] = json.loads(d["arguments"])
            except (json.JSONDecodeError, TypeError):
                d["arguments"] = {}
        return cls(**d)


class TraceIn(BaseModel):
    """Start a new trace."""
    agent: str = "unknown"
    task: str = ""
    session_id: str = ""


class Trace(BaseModel):
    """A complete trace record."""
    id: str = ""
    agent: str = ""
    task: str = ""
    session_id: str = ""
    started_at: str = ""
    finished_at: Optional[str] = None
    span_count: int = 0
    spans: list[Span] = Field(default_factory=list)


class ReplayRequest(BaseModel):
    """Replay a recorded span with optional argument overrides."""
    span_id: int
    arguments_override: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = "replay"
