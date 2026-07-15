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

import json
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
        # Write a START meta.json immediately so a crash BEFORE finalize still leaves a
        # parseable session (post-start fields stay None until finalize seals them).
        self._meta_path.write_text(start_meta.model_dump_json())

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

    def record_and_return_pair(self, event_line: dict[str, Any]) -> tuple[int, int]:
        """Append one event line exactly as :meth:`record` does, returning the PERSISTED
        ``(recv_ts, sequence_no)`` (REQ-020b/027).

        APPEND-ONLY R4-B wrapper over the UNCHANGED :meth:`record`: the returned pair is read back
        from the appended in-memory row (``self._events[-1]``), so it is the EXACT
        ``(recv_ts, sequence_no)`` written to ``records.jsonl`` — the live-mint decision boundary
        binds to the sealed tape pair, never a parallel guess. ``sequence_no`` is the recorder-minted
        GLOBAL sequence (the sole authority), NOT any placeholder carried on ``event_line``.
        """
        self.record(event_line)
        persisted = self._events[-1]
        return int(persisted["recv_ts"]), int(persisted["sequence_no"])

    def record_gap(self, from_ts: int, to_ts: int, source: str, reason: str) -> None:
        """Write an explicit, labeled gap marker line (never a silent splice).

        The gap draws the next monotonic ``sequence_no`` and is appended to the sealed
        in-memory event stream (``self._events``) so the finalized ``content_hash`` COVERS the
        gap window — a tampered gap line therefore breaks :func:`replay_reproduces`. Gaps
        remain honestly labeled ``RecorderGapEvent`` lines, so analysis (gap-crossing
        exclusion) is unaffected.
        """
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
        payload = gap.model_dump()
        self._write_line(payload)
        self._events.append(payload)

    def finalize(
        self,
        *,
        ended_ts: int,
        mapping_hash: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> LiveRecorderSessionMeta:
        """Seal and write ``meta.json`` (finalize_meta-style) at shutdown.

        Fills ``ended_ts``, ``event_count`` and the sealed ``content_hash`` (over the full
        recorded event stream — events AND gap markers) onto the start meta and persists it.
        The caller-supplied ``mapping_hash`` (fixture->token resolution provenance) and
        ``poll_interval_ms`` are recorded too, unless already present on the start meta.
        """
        update: dict[str, Any] = {
            "ended_ts": ended_ts,
            "event_count": len(self._events),
            "content_hash": session_content_hash(self._events),
        }
        if mapping_hash is not None:
            update["mapping_hash"] = mapping_hash
        if poll_interval_ms is not None:
            update["poll_interval_ms"] = poll_interval_ms
        meta = self._start_meta.model_copy(update=update)
        self._meta_path.write_text(meta.model_dump_json())
        return meta

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> LiveRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def resume_recorder(
    session_dir: str | Path, meta: LiveRecorderSessionMeta
) -> LiveRecorder:
    """Reopen a crash-partial session as an append-only writer, continuing the global sequence.

    APPEND-ONLY R4-B writer-resume opener (the legacy :class:`LiveRecorder` ``__init__`` /
    :meth:`record` / :meth:`_next_seq` / :meth:`finalize` are UNCHANGED — R3 tapes stay
    byte-identical). Implements EXACTLY the operator-approved NARROW crash model (REQ-020b/027 —
    "durable per session across process restarts"): the SAME crash-safe boundary as the R3 reader,
    NOT a general recovery system. A naive ``LiveRecorder(session_dir, meta)`` restart would reset
    ``_seq=0`` / ``_events=[]`` and reseal an INCOMPLETE stream (the on-disk duplicate ``[1, 1]``
    surfaces only at :func:`~veridex.live_recorder.replay.replay_reproduces`); this opener instead:

    (iv) FAILS CLOSED FIRST — before ANY truncation / writer-open / append / meta-write — on a
        FINALIZED or partially-finalized session, reconciling against the DURABLE on-disk
        ``meta.json`` (NOT merely the caller-supplied object). Resume recovers a process killed
        BEFORE ``finalize()``; a completed seal is TERMINAL evidence and must never be
        reopened/resealed. Both the durable meta AND the supplied object must be crash-partial START
        shape (``ended_ts`` / ``event_count`` / ``content_hash`` ALL absent), and the supplied object
        must MATCH the durable meta field-for-field — so a stale START claim can never bypass a
        durable seal and erase it via the START-meta rewrite in ``__init__``. Also raises on any
        malformed NON-final row, missing / duplicate / regressed ``sequence_no``, so a corrupted
        middle never resumes.
    (ii) VALIDATES the durable prefix (every ``sequence_no`` present, unique, strictly increasing)
        in one raw pass BEFORE any append.
    (iii) if the ONLY defect is a crash-truncated FINAL JSON line, PHYSICALLY truncates that
        incomplete tail (to the last complete ``\\n``) before appending — never appends onto
        malformed bytes (that would merge the next row onto the partial and make the tape unreadable).
        A COMPLETE-but-unterminated final record (valid JSON, trailing ``\\n`` lost in the crash) is
        instead FRAMED with a newline before append, so the next row never concatenates onto it.
    (i) HYDRATES ``_events`` with the COMPLETE valid durable prefix (events AND gap rows) via
        :func:`~veridex.live_recorder.replay.read_session_strict`, so ``finalize()`` seals the full
        pre+post-restart stream and ``replay_reproduces()`` stays True.
    (v) SEEDS ``_seq`` from the validated durable max (over ALL rows incl. a gap-at-tail) so the
        next appended row is ``max + 1``.

    The recorder owns ONLY the global sequence + stream state — NO epoch responsibility (the
    assembler is the sole author of per-source epochs). Reopening uses append mode and does NOT
    clobber ``meta.json`` or any valid row. **Out of scope (operator-pinned):** arbitrary-corruption
    repair, concurrent writers, multi-host recovery — the boundary is exactly R3's crash-safe model.
    """
    # Deferred import avoids a recorder<->replay import cycle (replay imports from recorder).
    from veridex.live_recorder.replay import max_sequence_no, read_session_strict

    session_dir = Path(session_dir)
    records_path = session_dir / RECORDS_FILENAME
    meta_path = session_dir / META_FILENAME

    # (iv) FAIL CLOSED FIRST — reconcile against the DURABLE on-disk meta.json, NOT the caller's
    # claim. A completed (or partial) seal is TERMINAL evidence: it must never be reopened or
    # resealed, and a stale START object supplied by the caller must NOT be able to erase a durable
    # seal via the START-meta rewrite in ``LiveRecorder.__init__``. So the finalized-session guard
    # reads DISK, and the supplied object must match the durable meta field-for-field before any
    # writer opens.
    disk_meta = LiveRecorderSessionMeta.model_validate_json(meta_path.read_text())
    for source_meta, origin in ((disk_meta, "durable meta.json"), (meta, "supplied meta")):
        if not (
            source_meta.ended_ts is None
            and source_meta.event_count is None
            and source_meta.content_hash is None
        ):
            raise ValueError(
                "resume_recorder refuses a finalized or partially-finalized session (terminal "
                f"evidence, from {origin}): ended_ts={source_meta.ended_ts!r}, "
                f"event_count={source_meta.event_count!r}, "
                f"content_hash={'<set>' if source_meta.content_hash is not None else None!r}"
            )
    if meta != disk_meta:
        raise ValueError(
            "resume_recorder: supplied meta is inconsistent with the durable meta.json on disk — "
            "refusing to rewrite session metadata from a mismatched object"
        )

    # (ii)+(iii) Validate the durable prefix and detect a crash-truncated FINAL line in ONE raw pass,
    # before opening the writer or appending anything.
    raw = records_path.read_bytes() if records_path.exists() else b""
    lines = raw.decode("utf-8").splitlines()
    last_index = len(lines) - 1
    truncated_tail = False
    prev_seq: int | None = None
    for i, line in enumerate(lines):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i != last_index:
                raise ValueError(
                    "resume_recorder: malformed NON-final durable row — only the final line may be "
                    "a crash-truncated partial write (arbitrary-corruption repair is out of scope)"
                ) from None
            truncated_tail = True
            continue
        if "sequence_no" not in entry:
            raise ValueError("resume_recorder: durable row is missing 'sequence_no'")
        seq = entry["sequence_no"]
        if prev_seq is not None and seq <= prev_seq:
            raise ValueError(
                "resume_recorder: durable sequence_no is not strictly increasing "
                f"(saw {seq!r} after {prev_seq!r}) — duplicate or regression"
            )
        prev_seq = seq

    # (iii) Physically truncate the crash-truncated final line to the last complete newline BEFORE
    # appending — never append onto malformed bytes.
    if truncated_tail:
        cut = raw.rfind(b"\n")
        records_path.write_bytes(raw[: cut + 1] if cut != -1 else b"")
    elif raw and not raw.endswith(b"\n"):
        # (iii-b) COMPLETE-but-unterminated final record: valid JSON whose trailing newline was lost
        # in the crash. Frame it with a newline BEFORE opening the append writer, so the next append
        # lands on its OWN line instead of concatenating onto the final object — which would make
        # read_session see one merged, unparseable final line and silently drop BOTH rows.
        records_path.write_bytes(raw + b"\n")

    # All fail-closed checks passed. Construct in append mode (does NOT clobber records.jsonl or
    # valid rows; rewrites meta.json with the SAME crash-partial START bytes).
    recorder = LiveRecorder(session_dir, meta)
    # (i) Hydrate the full valid durable prefix — events AND gap rows — in sequence order.
    _, events, gaps = read_session_strict(session_dir)
    recorder._events = sorted((*events, *gaps), key=lambda row: row["sequence_no"])
    # (v) Seed the global sequence from the durable max over ALL rows (incl. a gap-at-tail).
    recorder._seq = max_sequence_no(session_dir)
    return recorder
