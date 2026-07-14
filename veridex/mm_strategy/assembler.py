"""Recorder-global mint boundary + typed per-source epoch resume (MM-R4-B, milestone E3).

The adapter-tier seam between the durable live-recorder tape and the pure strategy core. Two
load-bearing trust rules pin this module (REQ-020b/027):

* **ONE global sequence authority.** Every source (book/FV/status/match-state/projection) is
  minted through the SAME :class:`~veridex.live_recorder.recorder.LiveRecorder`, inheriting one
  strictly-monotonic ``sequence_no``. This module NEVER mints its own sequence — it consumes the
  recorder-assigned ``(recv_ts, sequence_no)`` pair via
  :meth:`~veridex.live_recorder.recorder.LiveRecorder.record_and_return_pair`.
* **The assembler is the SOLE author of per-source epochs.** Generations live on the tape as
  ``MintEvent.source_epoch`` (never a ``meta.json`` field — that would break R3 meta byte-identity),
  so :func:`read_durable_source_generations` reads the durable max per source back from the tape and
  :func:`resume_source_generations` typed-increments it on an assembler restart.

Imports (E3-T5 audits this boundary): stdlib + the pure ``mm_strategy.contracts`` types + the
``veridex.live_recorder`` READ/resume/alignment surfaces ONLY. NEVER ``veridex.venues.base`` /
``veridex.venues.sx_bet`` — no submit / cancel / signer surface. (The live venue read seams
``polymarket_resolver`` / ``market_status`` are wired by the later E3-T4 cadence work.)
"""

from __future__ import annotations

from pathlib import Path

from veridex.live_recorder.alignment import FvPoint, eligible_fv_pair
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import read_session_strict
from veridex.mm_strategy.contracts import (
    MarketStatusEvent,
    MintEvent,
    SourceGenerations,
)

_MINT_EVENT_TYPE = "MintEvent"
_MARKET_STATUS_EVENT_TYPE = "MarketStatusEvent"


def mint(recorder: LiveRecorder, event: MintEvent) -> tuple[int, int]:
    """Mint one source observation through the single global recorder, returning its sealed pair.

    Records ``event`` (self-describing — ``event_type == "MintEvent"``) via
    :meth:`~veridex.live_recorder.recorder.LiveRecorder.record_and_return_pair`, so the assembler
    binds to the recorder-assigned GLOBAL ``(recv_ts, sequence_no)`` — never a locally-minted
    counter. The placeholder ``event.sequence_no`` is overridden by the recorder authority.
    """
    return recorder.record_and_return_pair(event.model_dump())


def sample_fv_into_mint(
    recorder: LiveRecorder, event: MintEvent, fv_cache: list[FvPoint]
) -> tuple[tuple[int, int], FvPoint | None]:
    """Mint one non-FV trigger and sample the FV cache at its SEALED global boundary (REQ-020(d2)).

    The single seam where a book/status/match/projection trigger pulls the aligned FV into its next
    observation WITHOUT look-ahead (AC-058 / RED-54). The trigger is minted through the ONE global
    recorder (:func:`mint`), which SEALS its global ``(recv_ts, sequence_no)`` to the tape; that
    recorder-assigned pair — never a locally-minted counter — is the visibility boundary handed to
    :func:`~veridex.live_recorder.alignment.eligible_fv_pair`. ``fv_cache`` is the RAW FV arrival
    history (corrections retained); sampling abstains (``None``) when nothing is visible below the
    boundary — the FV is never imputed.

    Returns the sealed ``(recv_ts, sequence_no)`` boundary AND the sampled ``FvPoint | None`` so the
    caller binds both into the next observation. The returned pair is EXACTLY the persisted tape pair
    — the live decision boundary IS the sealed replay boundary (the 3-way persist↔decide control).
    """
    mint_pair = mint(recorder, event)
    sampled = eligible_fv_pair(fv_cache, mint_pair[0], mint_pair[1])
    return mint_pair, sampled


def record_market_status(
    recorder: LiveRecorder, event: MarketStatusEvent
) -> tuple[int, int]:
    """Record one typed :class:`MarketStatusEvent` row through the single global recorder.

    Tagged with ``event_type == "MarketStatusEvent"`` so :func:`latest_market_status` can identify
    it on the durable tape. Returns the recorder-assigned ``(recv_ts, sequence_no)`` pair.
    """
    payload = {**event.model_dump(), "event_type": _MARKET_STATUS_EVENT_TYPE}
    return recorder.record_and_return_pair(payload)


def read_durable_source_generations(session_dir: str | Path) -> SourceGenerations:
    """Read the durable-max per-source generations from the tape's ``MintEvent`` rows.

    The SOLE per-source-epoch channel (Fable-plan-review-R4 Minor-1): scans the strict R4-B tape for
    ``MintEvent`` rows and takes the maximum ``source_epoch`` per source. ``book`` is the universal
    generation (defaults to ``0`` when the tape carries no book row yet); ``fv`` / ``market_status``
    are ``None`` when the tape has no such generation — mirroring guard-off (no FV epoch anywhere)
    and the ``UNKNOWN`` status sentinel. NEVER reads ``meta.json`` (it carries no epoch field).
    """
    _, events, _ = read_session_strict(session_dir)
    per_source: dict[str, int] = {}
    for row in events:
        if row.get("event_type") != _MINT_EVENT_TYPE:
            continue
        source = row["source"]
        epoch = row["source_epoch"]
        current = per_source.get(source)
        if current is None or epoch > current:
            per_source[source] = epoch
    return SourceGenerations(
        book_source_epoch=per_source.get("book", 0),
        fv_source_epoch=per_source.get("fv"),
        market_status_epoch=per_source.get("market_status"),
    )


def resume_source_generations(
    prior: SourceGenerations, *, guard_enabled: bool
) -> SourceGenerations:
    """Typed per-source generation resume on an assembler restart (REQ-020b/(d2)/(e)).

    A restart is a NEW generation. ``book_source_epoch`` ALWAYS increments (the universal source).
    ``fv_source_epoch`` increments IFF ``guard_enabled`` — guard-off leaves it ``None`` (no FV epoch
    anywhere, REQ-020(d2)). ``market_status_epoch`` increments IFF a prior market-status generation
    is present, else stays ``None``. The assembler is the sole author of these epochs.
    """
    # guard-off ⇒ NO fv epoch anywhere (REQ-020(d2)); status increments only when already present.
    fv_next = (prior.fv_source_epoch or 0) + 1 if guard_enabled else None
    status_next = (
        prior.market_status_epoch + 1 if prior.market_status_epoch is not None else None
    )

    return SourceGenerations(
        book_source_epoch=prior.book_source_epoch + 1,
        fv_source_epoch=fv_next,
        market_status_epoch=status_next,
    )


def latest_market_status(session_dir: str | Path) -> MarketStatusEvent:
    """The latest durable market status — ``UNKNOWN`` (fail closed) when no status row exists.

    Scans the strict R4-B tape for ``MarketStatusEvent`` rows and returns the one with the greatest
    global ``sequence_no`` as a typed :class:`MarketStatusEvent`. A tape WITHOUT any status row
    yields ``MarketStatusEvent(status="UNKNOWN", recv_ts=None, epoch=None)`` — the harness never
    silently synthesizes ``ACTIVE`` (REQ-027 / AC-053).
    """
    _, events, _ = read_session_strict(session_dir)
    status_rows = [
        row for row in events if row.get("event_type") == _MARKET_STATUS_EVENT_TYPE
    ]
    if not status_rows:
        return MarketStatusEvent(
            venue_market_ref="", status="UNKNOWN", recv_ts=None, epoch=None
        )
    latest = max(status_rows, key=lambda row: row["sequence_no"])
    return MarketStatusEvent(
        venue_market_ref=latest["venue_market_ref"],
        status=latest["status"],
        recv_ts=latest["recv_ts"],
        epoch=latest["epoch"],
    )
