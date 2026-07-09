"""E3 append-only live-recorder sink + session content hash (MM-R3, milestone E3).

Append-only session writer for the live-recorder lane. One JSON object per line; a
monotonic ``sequence_no`` is assigned in append order; a stream gap is written as a
LABELED :class:`~veridex.live_recorder.contracts.RecorderGapEvent` line — never a silent
splice. A finalized ``meta.json`` is written at shutdown.

This module reuses the confirmed primitives rather than reimplementing them:

* canonical line serialization delegates to
  :func:`veridex.runtime.evidence.serialize_payload`;
* the sealed session content hash delegates to
  :func:`veridex.runtime.evidence.compute_evidence_hash` (sequence-ordered canonical hash
  that RAISES on a duplicate ``sequence_no``);
* the on-disk layout mirrors ``veridex/ingest/recorder.py`` (``records.jsonl`` +
  ``meta.json``) and the gap line is modeled on ``ingest/recorder.py::gap_line``.

NO network, NO LLM import; imports nothing from ``veridex.scoring`` or ``veridex.maker``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from veridex.live_recorder.contracts import LiveRecorderSessionMeta, RecorderGapEvent
from veridex.runtime.evidence import compute_evidence_hash, serialize_payload

RECORDS_FILENAME = "records.jsonl"
META_FILENAME = "meta.json"


def session_content_hash(events: list[dict[str, Any]]) -> str:
    """Sealed session content hash over the sequence-ordered event stream.

    Delegates to :func:`veridex.runtime.evidence.compute_evidence_hash` (which canonically
    serializes via :func:`serialize_payload` and hashes the whole sorted array). Because the
    full event dict is hashed, any nested executability ``label`` present on an event is part
    of the hash. RAISES ``ValueError`` on a duplicate ``sequence_no`` (determinism guard).
    """
    return compute_evidence_hash(events)


class Recorder(Protocol):
    """The append-only recorder sink contract used by the live capture shell."""

    def record(self, event_line: dict[str, Any]) -> None: ...

    def record_gap(self, from_ts: int, to_ts: int, source: str, reason: str) -> None: ...


class LiveRecorder:
    """Append-only session recorder: one JSON line per event, explicit gap markers.

    Writes ``<session_dir>/records.jsonl`` (append-only) and, at :meth:`finalize`,
    ``<session_dir>/meta.json``. A monotonic ``sequence_no`` is assigned in append order and
    overrides any ``sequence_no`` carried on the incoming payload — the recorder is the sole
    authority for append order.
    """

    def __init__(self, session_dir: str | Path, start_meta: LiveRecorderSessionMeta) -> None:
        self._dir = Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._records_path = self._dir / RECORDS_FILENAME
        self._meta_path = self._dir / META_FILENAME
        self._start_meta = start_meta
        self._seq = 0
        self._events: list[dict[str, Any]] = []
        self._fh = self._records_path.open("a", encoding="utf-8")

    @property
    def records_path(self) -> Path:
        return self._records_path

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write_line(self, payload: dict[str, Any]) -> None:
        self._fh.write(serialize_payload(payload) + "\n")
        self._fh.flush()

    def record(self, event_line: dict[str, Any]) -> None:
        """Append one event line, assigning the next monotonic ``sequence_no``."""
        payload = {**event_line, "sequence_no": self._next_seq()}
        self._write_line(payload)
        self._events.append(payload)

    def record_gap(self, from_ts: int, to_ts: int, source: str, reason: str) -> None:
        """Write an explicit, labeled gap marker line (never a silent splice)."""
        gap = RecorderGapEvent(
            sequence_no=self._next_seq(),
            event_type="RecorderGapEvent",
            source_ts=None,
            recv_ts=to_ts,
            from_ts=from_ts,
            to_ts=to_ts,
            source=source,
            reason=reason,
        )
        self._write_line(gap.model_dump())

    def finalize(self, *, ended_ts: int) -> LiveRecorderSessionMeta:
        """Seal and write ``meta.json`` (finalize_meta-style) at shutdown.

        Fills ``ended_ts``, ``event_count`` and the sealed ``content_hash`` (over the
        recorded non-gap event stream) onto the start meta and persists it.
        """
        meta = self._start_meta.model_copy(
            update={
                "ended_ts": ended_ts,
                "event_count": len(self._events),
                "content_hash": session_content_hash(self._events),
            }
        )
        self._meta_path.write_text(meta.model_dump_json())
        return meta

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> LiveRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
