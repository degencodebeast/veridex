"""E3 crash-safe replay reader + deterministic re-hash (MM-R3, milestone E3).

Reads a sealed session back from disk and proves it replays deterministically. The reader
delegates to the crash-safe line loop pattern of ``veridex/ingest/recorder.py::read_session``:
a truncated FINAL line (process killed mid-write) is dropped without raising, while a
malformed NON-final line RAISES. Gap markers are honest, labeled
:class:`~veridex.live_recorder.contracts.RecorderGapEvent` lines and are returned separately
from the analysis event stream.

:func:`iter_change_series` groups events by ``(fixture_id, market_ref)`` and EXCLUDES any
consecutive change whose interval crosses a recorded gap — a change spanning a gap is not a
real continuous move and must not enter the analysis series.

:func:`replay_reproduces` recomputes the sealed content hash from the on-disk bytes and
asserts identity to the recorded ``content_hash`` — byte-determinism.

NO network, NO LLM import; imports nothing from ``veridex.scoring`` or ``veridex.maker``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import (
    META_FILENAME,
    RECORDS_FILENAME,
    session_content_hash,
)

__all__ = ["read_session", "iter_change_series", "replay_reproduces", "session_content_hash"]


def read_session(
    session_dir: str | Path,
) -> tuple[LiveRecorderSessionMeta, list[dict[str, Any]], list[dict[str, Any]]]:
    """Read ``<session_dir>/meta.json`` + ``<session_dir>/records.jsonl``.

    Returns ``(meta, events, gaps)`` where ``gaps`` are the ``RecorderGapEvent`` lines and
    ``events`` are all other appended lines.

    Crash-safe (mirrors ``ingest/recorder.py::read_session``): a truncated FINAL line is
    dropped without raising; a malformed NON-final line RAISES ``json.JSONDecodeError``.
    """
    session_dir = Path(session_dir)
    meta = LiveRecorderSessionMeta.model_validate_json(
        (session_dir / META_FILENAME).read_text()
    )

    events: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    lines = (session_dir / RECORDS_FILENAME).read_text().splitlines()
    for i, line in enumerate(lines):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                raise  # only the final line may be a crash-truncated partial write
            continue
        if entry.get("event_type") == "RecorderGapEvent":
            gaps.append(entry)
        else:
            events.append(entry)

    return meta, events, gaps


def _crosses_gap(prev_ts: int, curr_ts: int, gaps: list[dict[str, Any]]) -> bool:
    """True if the change interval ``[prev_ts, curr_ts]`` overlaps any gap window."""
    lo, hi = (prev_ts, curr_ts) if prev_ts <= curr_ts else (curr_ts, prev_ts)
    for gap in gaps:
        if not (gap["to_ts"] < lo or gap["from_ts"] > hi):
            return True
    return False


def iter_change_series(
    events: list[dict[str, Any]], gaps: list[dict[str, Any]]
) -> Iterator[tuple[tuple[int, str], dict[str, Any], dict[str, Any]]]:
    """Yield consecutive ``(key, prev_event, curr_event)`` changes per series, gap-safe.

    Events are grouped by ``key = (fixture_id, market_ref)`` (only events carrying both keys
    participate). Within a series, events are ordered by ``recv_ts`` (tie-break
    ``sequence_no``) and each consecutive pair is a change. A change whose interval crosses a
    recorded gap marker is EXCLUDED — never spliced across the gap.
    """
    series: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for event in events:
        if "fixture_id" not in event or "market_ref" not in event:
            continue
        key = (event["fixture_id"], event["market_ref"])
        series.setdefault(key, []).append(event)

    for key, group in series.items():
        ordered = sorted(group, key=lambda e: (e["recv_ts"], e["sequence_no"]))
        for prev, curr in zip(ordered, ordered[1:]):
            if _crosses_gap(prev["recv_ts"], curr["recv_ts"], gaps):
                continue
            yield key, prev, curr


def replay_reproduces(session_dir: str | Path) -> bool:
    """True iff the content hash recomputed from the sealed bytes matches the recorded one.

    Reads the session back, recomputes :func:`session_content_hash` over the on-disk event
    stream (which includes any nested executability labels), and compares to the sealed
    ``meta.content_hash``. Proves the replay is byte-deterministic.
    """
    meta, events, _gaps = read_session(session_dir)
    return meta.content_hash is not None and meta.content_hash == session_content_hash(events)
