"""Minimal SQLite store for spike runs/events/artifacts. Test-driven (T6)."""

from __future__ import annotations

from typing import Any


def init_db(path: str = ":memory:") -> Any:
    """Create the minimal spike schema (runs, run_events, raw_prescore_records, artifacts)."""
    raise NotImplementedError("T6: sqlite store")
