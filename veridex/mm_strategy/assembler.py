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
``veridex.venues.sx_bet`` — no submit / cancel / signer surface. The FV-independent cadence engine
(:func:`run_cadence` / :func:`project_guard_fv`) folds INJECTED observation facts and imports the
``mm_strategy.contracts`` types + ``veridex.live_recorder`` ONLY — never ``mm_strategy.core`` /
``mm_strategy.config`` / a venue surface — so decision/state identity is a downstream consequence of
the identical observation stream (a valid replay reproduces it), not an assembler import.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from veridex.live_recorder.alignment import FvPoint, eligible_fv_pair
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import read_session_strict
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    MarketStatusEvent,
    MintEvent,
    MintSource,
    ProofStatus,
    SourceGenerations,
    StrategyObservation,
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


# --- FV-independent cadence + guard-off projection (REQ-020(d/d2) / AC-049/051/056) ---------
# The load-bearing A/B baseline-arm integrity seam. Two invariants pin this section:
#   * GUARD-OFF PROJECTION — with the guard config-disabled the assembler emits ``guard_fv=None`` on
#     EVERY observation regardless of FV feed health, so no fv value / ts / epoch enters a baseline
#     observation (or, via ``decide``, its state). The baseline observation/decision/state stream is
#     therefore BYTE-IDENTICAL across healthy / stale / absent / reconnecting FV — E3-T5/E6 rely on
#     this ``guard_enabled=False`` byte-identity guarantee.
#   * FV-INDEPENDENT CADENCE — observations are minted ONLY by book/status/match-state/projection
#     events. An FV arrival updates the single-authority latest-value cache (minted through the ONE
#     global recorder so its ``sequence_no`` is comparable to a trigger pair, per E3-T3) but NEVER
#     mints an observation nor advances ``observation_sequence``, in EITHER arm.


ObservationFactory = Callable[[int, GuardFairValue | None], StrategyObservation]
"""Builds one observation from the cadence-assigned ``observation_sequence`` and the projected guard
leg (``None`` in the baseline arm). The driver is the SOLE author of both; the caller supplies the RAW
venue / match-state facts (arm-IDENTICAL), so the guard leg is the ONLY arm-dependent input."""


@dataclass(frozen=True)
class FvArrival:
    """A raw FV arrival fed into the single-authority latest-value cache (REQ-020(d2) / REQ-027).

    Minting an :class:`FvArrival` updates the cache the GUARDED arm samples but NEVER mints an
    observation nor advances ``observation_sequence`` — the FV leg has ZERO cadence authority in
    either arm. ``message_id`` / ``proof_status`` ride into the guard leg only when the guard is on.
    """

    source_ts: int
    recv_ts: int
    value: float
    source_epoch: int
    message_id: str | None = None
    proof_status: ProofStatus = "unavailable_no_message_id"


@dataclass(frozen=True)
class ObservationTick:
    """A book / market-status / match-state / projection event that MINTS exactly one observation.

    ``source`` MUST be a non-FV mint source — an FV trigger can never mint an observation
    (REQ-020(d2)); construction fails closed otherwise. ``build`` is invoked with the cadence-assigned
    ``observation_sequence`` and the projected guard leg to produce the fully-validated observation.
    """

    source: MintSource
    source_epoch: int
    recv_ts: int
    build: ObservationFactory

    def __post_init__(self) -> None:
        if self.source == "fv":
            raise ValueError(
                "an FV source can never mint an observation (REQ-020(d2)); use FvArrival"
            )


CadenceEvent = FvArrival | ObservationTick


@dataclass(frozen=True)
class _CachedFv:
    """One raw FV arrival sealed at its global recorder pair — the guard-scoped cache entry. Carries
    the FV epoch / proof reference the guarded projection folds into a :class:`GuardFairValue`."""

    point: FvPoint
    source_epoch: int
    message_id: str | None
    proof_status: ProofStatus


@dataclass(frozen=True)
class CadenceRun:
    """The minted observation stream plus each observation's SEALED global ``(recv_ts, sequence_no)``
    mint pair (audit). ``len(mint_pairs) == len(observations)`` — an FV arrival contributes neither."""

    observations: tuple[StrategyObservation, ...]
    mint_pairs: tuple[tuple[int, int], ...]


def project_guard_fv(
    fv_cache: list[_CachedFv], mint_pair: tuple[int, int], *, guard_enabled: bool
) -> GuardFairValue | None:
    """Project the guard leg for one observation — the canonical guard-off rule (REQ-020(d2)).

    Guard-OFF (``guard_enabled=False``) ALWAYS returns ``None`` WITHOUT reading ``fv_cache``: no fv
    value / ts / epoch can enter a baseline observation, so the baseline stream is byte-identical
    across FV feed health BY CONSTRUCTION (the cache is literally unobservable in the baseline arm).
    Guard-ON samples the single-authority cache at the SEALED global ``mint_pair`` via
    :func:`~veridex.live_recorder.alignment.eligible_fv_pair` (no look-ahead) and abstains (``None``)
    when nothing is visible below the boundary — the FV is never imputed.
    """
    if not guard_enabled:
        return None
    winner = eligible_fv_pair(
        [entry.point for entry in fv_cache], mint_pair[0], mint_pair[1]
    )
    if winner is None:
        return None
    cached = next(entry for entry in fv_cache if entry.point is winner)
    return GuardFairValue(
        fv=cached.point.value,
        fv_source_ts=cached.point.source_ts,
        fv_recv_ts=cached.point.recv_ts,
        fv_source_epoch=cached.source_epoch,
        message_id=cached.message_id,
        proof_status=cached.proof_status,
    )


def run_cadence(
    recorder: LiveRecorder, events: Iterable[CadenceEvent], *, guard_enabled: bool
) -> CadenceRun:
    """Fold a heterogeneous event stream into the minted observation stream (REQ-020(d)/(d2)).

    Every event rides the ONE global ``recorder`` (the single sequence authority), but only a non-FV
    :class:`ObservationTick` MINTS an observation and advances ``observation_sequence``; an
    :class:`FvArrival` updates the latest-value cache alone (no observation, no sequence advance — in
    EITHER arm). Each minted observation binds the cadence-assigned sequence and the
    :func:`project_guard_fv` leg (``None`` in the baseline arm) onto the tick's arm-IDENTICAL
    ``build`` facts, so a guard-off run yields a byte-identical observation stream regardless of FV
    feed health. Decision/state identity follows downstream from feeding that stream to ``decide``.
    """
    observation_sequence = 0
    fv_cache: list[_CachedFv] = []
    observations: list[StrategyObservation] = []
    mint_pairs: list[tuple[int, int]] = []
    for event in events:
        if isinstance(event, FvArrival):
            # FV arrival: seal it on the global tape and cache it — but mint NO observation and do
            # NOT advance observation_sequence (latest-value cache only, both arms).
            pair = mint(
                recorder,
                MintEvent(
                    sequence_no=0,
                    source="fv",
                    source_epoch=event.source_epoch,
                    recv_ts=event.recv_ts,
                ),
            )
            fv_cache.append(
                _CachedFv(
                    point=FvPoint(
                        source_ts=event.source_ts,
                        recv_ts=pair[0],
                        value=event.value,
                        sequence_no=pair[1],
                    ),
                    source_epoch=event.source_epoch,
                    message_id=event.message_id,
                    proof_status=event.proof_status,
                )
            )
            continue
        # Non-FV trigger: MINT one observation. The cadence sequence advances here (never on FV).
        observation_sequence += 1
        mint_pair = mint(
            recorder,
            MintEvent(
                sequence_no=0,
                source=event.source,
                source_epoch=event.source_epoch,
                recv_ts=event.recv_ts,
            ),
        )
        guard_fv = project_guard_fv(fv_cache, mint_pair, guard_enabled=guard_enabled)
        observations.append(event.build(observation_sequence, guard_fv))
        mint_pairs.append(mint_pair)
    return CadenceRun(observations=tuple(observations), mint_pairs=tuple(mint_pairs))


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
