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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import (
    META_FILENAME,
    RECORDS_FILENAME,
    session_content_hash,
)

__all__ = [
    "read_session",
    "read_session_strict",
    "max_sequence_no",
    "iter_change_series",
    "replay_reproduces",
    "session_content_hash",
]


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


def read_session_strict(
    session_dir: str | Path,
) -> tuple[LiveRecorderSessionMeta, list[dict[str, Any]], list[dict[str, Any]]]:
    """Strict R4-B reader: like :func:`read_session` but FAILS CLOSED on a missing ``sequence_no``.

    APPEND-ONLY R4-B addition (existing :func:`read_session` UNCHANGED for R3 back-compat). Same
    crash-safe line loop — a truncated FINAL line is dropped, a malformed NON-final line RAISES —
    but any NON-gap event row lacking ``sequence_no`` RAISES ``ValueError`` instead of being
    returned. The R4-B mint boundary reads through the single global ``sequence_no`` authority, so a
    row that cannot carry the global pair must never enter the replay stream (gap rows always carry
    their own ``sequence_no`` from :meth:`LiveRecorder.record_gap`, so they stay permitted).
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
            if "sequence_no" not in entry:
                raise ValueError(
                    "read_session_strict: non-gap event row is missing 'sequence_no' — the R4-B "
                    "mint boundary requires the global sequence on every recorded event row"
                )
            events.append(entry)

    return meta, events, gaps


def max_sequence_no(session_dir: str | Path) -> int:
    """The durable-maximum ``sequence_no`` over ALL rows — EVENTS AND GAP MARKERS.

    APPEND-ONLY R4-B durable-tail helper used by :func:`~veridex.live_recorder.recorder.resume_recorder`
    to seed the next global sequence. Reuses :func:`read_session`'s crash-safe loop so a truncated
    FINAL line is tolerated (dropped) while a malformed NON-final line RAISES — distinguishing the
    recoverable tail from a fatal middle corruption. Because :meth:`LiveRecorder.record_gap` draws
    its ``sequence_no`` from the SAME global ``_next_seq()``, a tape whose highest-sequence row is a
    gap must still seed correctly; scanning events ONLY would seed BELOW a gap-at-tail and collide.
    Returns ``0`` for an empty tape (no rows recorded before the crash).
    """
    _, events, gaps = read_session(session_dir)
    seqs = [row["sequence_no"] for row in (*events, *gaps) if "sequence_no" in row]
    return max(seqs) if seqs else 0


def _crosses_gap(prev_ts: int, curr_ts: int, gaps: list[dict[str, Any]]) -> bool:
    """True if the change interval ``[prev_ts, curr_ts]`` overlaps any gap window."""
    lo, hi = (prev_ts, curr_ts) if prev_ts <= curr_ts else (curr_ts, prev_ts)
    return any(not (gap["to_ts"] < lo or gap["from_ts"] > hi) for gap in gaps)


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
        for prev, curr in zip(ordered, ordered[1:], strict=False):
            if _crosses_gap(prev["recv_ts"], curr["recv_ts"], gaps):
                continue
            yield key, prev, curr


def replay_reproduces(session_dir: str | Path) -> bool:
    """True iff the content hash recomputed from the sealed bytes matches the recorded one.

    Reads the session back, recomputes :func:`session_content_hash` over the FULL on-disk
    stream — non-gap events AND gap markers together (``compute_evidence_hash`` re-sorts by
    ``sequence_no``, reconstructing the sealed order) — and compares to the sealed
    ``meta.content_hash``. Because gaps are part of the seal, a tampered gap window changes
    the recomputed hash and this returns False. Proves the replay is byte-deterministic.
    """
    meta, events, gaps = read_session(session_dir)
    full_stream = events + gaps
    return meta.content_hash is not None and meta.content_hash == session_content_hash(
        full_stream
    )
