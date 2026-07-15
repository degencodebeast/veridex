"""TEST-SIDE A/B ablation harness — shared venue-only core, arm identity, decision parity (E6-T1).

Covers REQ-110 / AC-001 / AC-002 / RED-23 / RED-24. This module is the honesty spine of the whole A/B
claim: it proves ONE strategy core drives BOTH arms, the guard is a SINGLE config-gated block, and the
ONLY thing distinguishing arm A (``guard_enabled=False``) from arm B (``guard_enabled=True``) is that
block.

It does NOT contain a second copy of the policy. It imports the veridex core to RUN the arms:
  * ``veridex.mm_strategy.core.decide`` — the ONE pure reducer both arms fold. The clock is an EXPLICIT
    per-observation input (``as_of_ts``); ``decide`` never reads a wall-clock (AC-002).
  * ``veridex.mm_strategy.assembler.run_cadence`` — the E3-T4 FV-independent cadence + guard-off
    projection. Guard-off emits ``guard_fv=None`` on every observation WITHOUT reading the FV cache, so
    the baseline observation stream is byte-identical across FV feed health BY CONSTRUCTION. RED-23
    leans directly on this contract; the harness does not re-derive it.
  * ``veridex.live_recorder.replay.{read_session, iter_change_series, replay_reproduces}`` — the
    replay reader the harness consumes to prove the sealed on-disk tape re-hashes deterministically.

TEST-SIDE, by construction and by test: this module lives under ``tests/`` and is imported ONLY by the
ablation tests. It imports NOTHING from ``veridex.research`` / ``veridex.maker`` / any ranked lane —
:func:`forbidden_import_hits` statically enforces that, and ``test_harness_is_test_side_no_ranked_import``
asserts it. Production / ranked code never imports this helper.

Fixtures (``tests/fixtures/mm_strategy/``) are canned, offline, deterministic JSON: a shared base config
override set (``base_config.json``, WITHOUT ``guard_enabled``) and one replay tape per FV-health variant
(``tape_{healthy,stale,absent}.json``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import (
    iter_change_series,
    read_session,
    replay_reproduces,
)
from veridex.mm_strategy.assembler import (
    AssemblerOwnedFacts,
    FvArrival,
    ObservationTick,
    run_cadence,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    CALIBRATION_LABEL,
    EDGE_LABEL,
    EVIDENCE_CLASS,
    RUN_LABEL,
    STRATEGY_REVISION,
    GuardFairValue,
    InventoryProjection,
    ReasonCode,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
    StreamIdentity,
)
from veridex.mm_strategy.core import decide

__all__ = [
    "FIXTURES_DIR",
    "FORBIDDEN_REACH_NULLS",
    "FORBIDDEN_SINGLE_METRICS",
    "HARNESS_MODULE_PATH",
    "MARKOUT_REFERENCE",
    "OBSERVED_MARKET_PRINT",
    "OWN_RECONCILED_FILL",
    "PERMITTED_CONCLUSION_SHAPE",
    "PROMOTED_EVIDENCE_CLASSES",
    "SIX_METRIC_KEYS",
    "TRIGGER_REASON_CODES",
    "AblationConclusion",
    "ArmConfigs",
    "ArmSingleMetrics",
    "CandidateFill",
    "CounterfactualCapacityCeiling",
    "ForbiddenReachNullError",
    "LoadedTape",
    "MarkoutReferenceError",
    "MatchedOpportunityReport",
    "ObservedMarketPrint",
    "ReplayResult",
    "RunReceipt",
    "ablation_conclusion",
    "arm_configs",
    "arm_single_metrics",
    "config_field_diff",
    "counterfactual_capacity_ceiling",
    "forbidden_import_hits",
    "load_base_config_overrides",
    "load_tape",
    "matched_opportunity_report",
    "mint_run_receipt",
    "neutralize_guard",
    "print_derived_trigger",
    "replay_arm",
    "reviewed_reach_baseline",
    "venue_future_mid_series",
]

HARNESS_MODULE_PATH = Path(__file__).resolve()
FIXTURES_DIR = HARNESS_MODULE_PATH.parent / "fixtures" / "mm_strategy"

# Ranked / non-venue import roots this TEST-SIDE harness must NEVER pull in. The baseline arm's
# venue-only purity would be a lie if the harness could reach a ranked lane or the LLM/maker tiers.
_FORBIDDEN_IMPORT_ROOTS = (
    "veridex.research",
    "veridex.maker",
    "veridex.scoring",
    "veridex.ranked",
)


# --- arm configuration: ONE base, the guard flag is the only lever ---------------------------


@dataclass(frozen=True)
class ArmConfigs:
    """The A/B config pair derived from ONE shared base override set.

    ``baseline`` is arm A (``guard_enabled=False``); ``guarded`` is arm B (``guard_enabled=True``).
    Every OTHER knob is identical by construction — both are built from the same ``overrides`` mapping.
    """

    baseline: StrategyConfig
    guarded: StrategyConfig


def load_base_config_overrides(fixtures_dir: Path = FIXTURES_DIR) -> dict[str, Any]:
    """Load the shared ``StrategyConfig`` knob overrides (``base_config.json``) — no ``guard_enabled``.

    The fixture deliberately omits ``guard_enabled`` so it can never leak into the "everything else is
    identical" half of the arm-identity claim; the harness supplies that one knob per arm.
    """
    payload = json.loads((fixtures_dir / "base_config.json").read_text())
    overrides = dict(payload["overrides"])
    if "guard_enabled" in overrides:
        raise ValueError(
            "base_config.json overrides must NOT set guard_enabled — it is the ONLY knob the "
            "harness flips between arms (REQ-110)"
        )
    return overrides


def arm_configs(overrides: dict[str, Any]) -> ArmConfigs:
    """Build arm A (guard-off) and arm B (guard-on) from ONE shared override set.

    The two configs are constructed from the SAME ``overrides`` mapping and differ ONLY by
    ``guard_enabled``. This is the structural guarantee the arm-identity test verifies: any second
    differing knob (e.g. the ``half_spread`` mutation) is a defect that surfaces immediately in
    :func:`config_field_diff`.
    """
    baseline = StrategyConfig(guard_enabled=False, **overrides)
    guarded = StrategyConfig(guard_enabled=True, **overrides)
    return ArmConfigs(baseline=baseline, guarded=guarded)


def config_field_diff(
    a: StrategyConfig, b: StrategyConfig
) -> dict[str, tuple[object, object]]:
    """Field-by-field diff of two configs — ``{field: (a_value, b_value)}`` for every differing knob.

    Diffs the canonical ``model_dump()`` of each config. For a valid A/B pair the result is exactly
    ``{"guard_enabled": (False, True)}``; any other key means a knob other than the guard block differs.
    """
    dump_a = a.model_dump()
    dump_b = b.model_dump()
    keys = set(dump_a) | set(dump_b)
    return {
        key: (dump_a.get(key), dump_b.get(key))
        for key in keys
        if dump_a.get(key) != dump_b.get(key)
    }


def neutralize_guard(config: StrategyConfig) -> StrategyConfig:
    """Return a copy with the guard block set to a canonical value, for hash-level identity checks.

    Two arms that differ ONLY in the guard block become byte-identical (same ``config_hash``) once the
    guard flag is neutralized. If any other knob differs, the neutralized hashes still diverge.
    """
    return config.model_copy(update={"guard_enabled": True})


# --- canned tape loading ---------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedTape:
    """A deserialized canned replay tape: the ordered cadence events + the stream/venue identity."""

    health: str
    identity: StreamIdentity
    venue_market_ref: str
    events: tuple[FvArrival | ObservationTick, ...]


def _observation(
    *,
    observation_sequence: int,
    guard_fv: GuardFairValue | None,
    as_of_ts: int,
    identity: StreamIdentity,
    venue_market_ref: str,
    book_source_epoch: int = 1,
    bid: float = 0.49,
    ask: float = 0.51,
    bid_size: float = 100.0,
    ask_size: float = 120.0,
) -> StrategyObservation:
    """One healthy per-tick observation — arm-IDENTICAL venue/match-state facts; only ``guard_fv`` is
    arm-dependent. Mirrors the E3-T4 honest builder so ``run_cadence``'s field-for-field authentication
    passes. Every ``recv_ts`` is ``<= as_of_ts`` (REQ-022 guard).

    ``bid``/``ask``/sizes default to the fixed microstructure the E6-T1/T2 tapes rely on (their rows
    omit these keys, so those streams stay byte-identical). A tape MAY carry per-tick ``bid``/``ask`` to
    STEP the venue mid — the E6-T3 markout tape does this so an event-time markout horizon (the next
    venue change) exists; the venue book is still arm-identical (both arms see the same mid)."""
    recv = as_of_ts - 10
    return StrategyObservation(
        fixture_id=identity.fixture_id,
        market_ref=identity.market_ref,
        side=identity.side,
        token_id=identity.token_id,
        venue_market_ref=venue_market_ref,
        tick_size=0.01,
        observation_sequence=observation_sequence,
        book_source_epoch=book_source_epoch,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        book_status="ok",
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=guard_fv,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=1,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _owned(*, as_of_ts: int) -> AssemblerOwnedFacts:
    """The assembler-owned facts EXACTLY matching :func:`_observation`, so authentication passes."""
    recv = as_of_ts - 10
    return AssemblerOwnedFacts(
        book_source_epoch=1,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=1,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
    )


def _tick(
    raw: dict[str, Any], identity: StreamIdentity, venue_market_ref: str
) -> ObservationTick:
    """Build a non-FV :class:`ObservationTick` from a raw tape row. The ``build`` factory binds the
    cadence-assigned sequence + projected guard leg onto arm-identical venue facts.

    ``bid``/``ask``/sizes are OPTIONAL on the row and default to the E6-T1/T2 fixed microstructure, so
    tapes that omit them are unchanged; the E6-T3 markout tape supplies them to step the venue mid."""
    as_of_ts = int(raw["as_of_ts"])
    bid = float(raw.get("bid", 0.49))
    ask = float(raw.get("ask", 0.51))
    bid_size = float(raw.get("bid_size", 100.0))
    ask_size = float(raw.get("ask_size", 120.0))

    def build(
        observation_sequence: int, guard_fv: GuardFairValue | None
    ) -> StrategyObservation:
        return _observation(
            observation_sequence=observation_sequence,
            guard_fv=guard_fv,
            as_of_ts=as_of_ts,
            identity=identity,
            venue_market_ref=venue_market_ref,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        )

    return ObservationTick(
        source=raw["source"],
        source_epoch=1,
        recv_ts=int(raw["recv_ts"]),
        owned=_owned(as_of_ts=as_of_ts),
        identity=identity,
        build=build,
    )


def _fv(raw: dict[str, Any], identity: StreamIdentity) -> FvArrival:
    """Build an :class:`FvArrival` from a raw tape row — feeds the latest-value cache, mints nothing."""
    return FvArrival(
        source_ts=int(raw["source_ts"]),
        recv_ts=int(raw["recv_ts"]),
        value=float(raw["value"]),
        source_epoch=int(raw["source_epoch"]),
        identity=identity,
    )


def load_tape(health: str, fixtures_dir: Path = FIXTURES_DIR) -> LoadedTape:
    """Load and deserialize the canned ``tape_{health}.json`` into typed cadence events (offline)."""
    payload = json.loads((fixtures_dir / f"tape_{health}.json").read_text())
    stream = payload["stream"]
    identity = StreamIdentity(
        fixture_id=int(stream["fixture_id"]),
        market_ref=stream["market_ref"],
        side=stream["side"],
        token_id=stream["token_id"],
    )
    venue_market_ref = payload["venue_market_ref"]
    events: list[FvArrival | ObservationTick] = []
    for raw in payload["events"]:
        kind = raw["kind"]
        if kind == "fv":
            events.append(_fv(raw, identity))
        elif kind == "tick":
            events.append(_tick(raw, identity, venue_market_ref))
        else:
            raise ValueError(f"unknown tape event kind: {kind!r}")
    return LoadedTape(
        health=payload["health"],
        identity=identity,
        venue_market_ref=venue_market_ref,
        events=tuple(events),
    )


# --- the A/B replay + diagnostics ------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """The diagnostics of one arm's replay over one tape.

    ``decisions`` is the folded decision stream (equality-comparable); ``decisions_digest`` is its
    canonical byte digest. ``observation_hashes`` / ``state_hash`` are the minted-stream + terminal-state
    identities. ``change_series`` is the ``iter_change_series`` reconstruction from the sealed tape and
    ``byte_reproduces`` is ``replay_reproduces`` over it — the replay.py consumption.
    """

    observations: tuple[StrategyObservation, ...]
    observation_hashes: tuple[str, ...]
    decisions: tuple[StrategyDecision, ...]
    decisions_digest: str
    final_state: StrategyState
    state_hash: str
    change_series: tuple[Any, ...]
    byte_reproduces: bool


def _start_meta(config: StrategyConfig, identity: StreamIdentity) -> LiveRecorderSessionMeta:
    """A deterministic session meta bound to the arm's ``config_hash`` (offline, no wall-clock)."""
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="e6t1-ablation-harness",
        config_hash=config.config_hash(),
        source_provenance={"venue": "poly"},
        fixture_ids=(identity.fixture_id,),
    )


def _decisions_digest(decisions: tuple[StrategyDecision, ...]) -> str:
    """A canonical byte digest of the decision stream — the concrete 'byte-identical' artifact."""
    canonical = json.dumps(
        [d.model_dump() for d in decisions], sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_arm(
    tape: LoadedTape, config: StrategyConfig, session_dir: Path
) -> ReplayResult:
    """Run ONE arm over ONE tape: mint the cadence, fold ``decide``, seal + re-read the tape.

    ``run_cadence`` folds the tape into the minted observation stream under ``config.guard_enabled`` —
    the guard-off projection emits ``guard_fv=None`` without reading the FV cache (E3-T4). Each
    observation is threaded through the ONE pure ``decide`` core (clock = the observation's explicit
    ``as_of_ts``), producing the decision stream + terminal state. Alongside the ``MintEvent`` cadence
    rows the recorder writes one self-describing ``BookChangeRow`` per observation (top-level
    ``fixture_id`` / ``market_ref`` / ``recv_ts``) so ``iter_change_series`` can reconstruct the change
    series on read-back; the session is finalized (sealing ``content_hash``) and ``replay_reproduces``
    confirms it re-hashes byte-identically.
    """
    recorder = LiveRecorder(session_dir, _start_meta(config, tape.identity))
    run = run_cadence(recorder, tape.events, guard_enabled=config.guard_enabled)
    # Emit a self-describing book-change replay row per minted observation. These carry TOP-LEVEL
    # fixture_id / market_ref so iter_change_series groups them; MintEvent rows nest identity and are
    # skipped, so the two row kinds never collide on the same tape.
    for obs in run.observations:
        # bid/ask are ``float | None`` on a stale book; a mid is only meaningful when both are present.
        mid = (
            (obs.bid + obs.ask) / 2.0
            if obs.bid is not None and obs.ask is not None
            else None
        )
        recorder.record_and_return_pair(
            {
                "event_type": "BookChangeRow",
                "fixture_id": obs.fixture_id,
                "market_ref": obs.market_ref,
                "recv_ts": obs.book_recv_ts,
                "mid": mid,
                "observation_sequence": obs.observation_sequence,
            }
        )
    recorder.finalize(ended_ts=10_000)
    recorder.close()

    # Fold the ONE pure decide core over the minted stream — clock is the explicit as_of_ts input.
    state = StrategyState()
    decisions: list[StrategyDecision] = []
    for obs in run.observations:
        decision, state = decide(obs, state, config)
        decisions.append(decision)

    # Consume replay.py: read the sealed tape back, reconstruct the change series, verify byte-determinism.
    _meta, events, gaps = read_session(session_dir)
    change_series = tuple(iter_change_series(events, gaps))
    byte_reproduces = replay_reproduces(session_dir)

    decisions_tuple = tuple(decisions)
    return ReplayResult(
        observations=run.observations,
        observation_hashes=tuple(o.observation_hash() for o in run.observations),
        decisions=decisions_tuple,
        decisions_digest=_decisions_digest(decisions_tuple),
        final_state=state,
        state_hash=state.state_hash(),
        change_series=change_series,
        byte_reproduces=byte_reproduces,
    )


# --- test-side purity guard ------------------------------------------------------------------


def forbidden_import_hits() -> list[str]:
    """Return any forbidden ranked/research/maker import statements in THIS module's own source.

    A static scan of the harness source (not a runtime import graph) for ``import``/``from`` lines that
    reference a forbidden root. An empty list proves the venue-only baseline arm cannot reach a ranked
    lane through the harness. The tuple ``_FORBIDDEN_IMPORT_ROOTS`` string literals here are NOT imports
    and are excluded by the ``import``/``from`` line anchor.
    """
    source = HARNESS_MODULE_PATH.read_text()
    hits: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for root in _FORBIDDEN_IMPORT_ROOTS:
            if re.search(rf"\b{re.escape(root)}\b", stripped):
                hits.append(stripped)
    return hits


# --- E6-T3: six-metric matched-opportunity report, venue-derived reference -------------------
# The evaluation report that would SCORE the two arms (REQ-112/113/AC-027/RED-26). Two trust rules:
#   (1) the SIX mandatory metrics are reported ALWAYS TOGETHER (no favorable subset alone) — a
#       flattering per-fill number can never travel without matched-opportunity, abstention, exposure;
#   (2) every markout is scored against the VENUE's OWN future mid at the next venue change (event-time,
#       step-function replay), NEVER the TxLINE FV the guard consumes. Scoring the FV-driven guard
#       against the same FV is circular self-validation, so an FV-referenced markout FAILS CLOSED.
# This is offline diagnostics over an already-replayed arm pair — NO ranked/research/maker import (the
# ``forbidden_import_hits`` guard still holds); the markout math mirrors ``veridex.maker.markout`` in
# shape but is re-implemented here so the harness never depends on the maker tier.

# The ONE permitted markout reference. The mutation for RED-26 flips this to ``"fv"``; the fail-closed
# guard below keys on the LITERAL ``"venue"``, so that flip makes the default report fail closed and
# ``test_markout_reference_is_venue_not_fv`` goes red.
MARKOUT_REFERENCE = "venue"

# The SIX mandatory metrics (REQ-112). Field names are venue/execution-flavored on purpose — none is a
# ranked-lane term (no score/rank/edge/confidence/alpha), so the E7-T1 diagnostic denylist stays clean.
SIX_METRIC_KEYS = frozenset(
    {
        "per_fill_markout",
        "matched_opportunity_markout",
        "exposure_normalized_adverse_selection",
        "fill_count",
        "abstention_count",
        "capital_at_risk",
    }
)

# Decision kinds that rest a priced quote leg (a candidate fill / eligible opportunity).
_QUOTE_LEG_ROLES = ("bid", "ask")


class MarkoutReferenceError(ValueError):
    """Raised when a markout would be scored against a non-venue (e.g. FV) reference (REQ-113/RED-26).

    Fail-closed: scoring the FV-driven guard against the SAME FV is circular self-validation, so the
    harness REFUSES to compute an FV-referenced markout rather than silently returning a circular score.
    """


@dataclass(frozen=True)
class CandidateFill:
    """One resting quote leg scored against the venue's OWN future mid at the next venue change.

    ``observation_index`` is where the leg rested; ``horizon_index`` is the EVENT-TIME markout horizon —
    the NEXT observation whose venue mid DIFFERS (a venue change, not a fixed wall-clock offset).
    ``markout_bps`` is signed by side (a bid gains when the venue rises above the quote, an ask when it
    falls below) and referenced ONLY to ``venue_future_mid`` — never to the TxLINE FV the guard consumes.
    Every resting quote is a candidate here: this is the honest CEILING model (no queue / no own-fill
    claim), the same evidence ceiling Gate B pins for reach/print/capacity.
    """

    observation_index: int
    horizon_index: int
    leg_role: str
    quote_price: float
    venue_now: float
    venue_future_mid: float
    markout_bps: int


@dataclass(frozen=True)
class MatchedOpportunityReport:
    """The SIX mandatory evaluation metrics, reported ALWAYS TOGETHER (REQ-112/AC-027).

    :meth:`metrics` returns EXACTLY :data:`SIX_METRIC_KEYS`, so no favorable subset (better per-fill
    markout, fewer fills) can be exposed on its own — the honest denominators (matched-opportunity
    delta over the SAME eligible opportunities, abstention count, capital at risk) always travel with
    it. Every markout is venue-derived (``markout_reference == "venue"``); the FV the guard consumes is
    NEVER the evaluation reference. ``fills`` is the per-fill detail behind ``per_fill_markout``.
    """

    per_fill_markout: float
    matched_opportunity_markout: float
    exposure_normalized_adverse_selection: float
    fill_count: int
    abstention_count: int
    capital_at_risk: float
    markout_reference: str
    matched_opportunity_count: int
    fills: tuple[CandidateFill, ...]

    def metrics(self) -> dict[str, float]:
        """The SIX mandatory metrics as ONE mapping — EXACTLY ``SIX_METRIC_KEYS``, no subset (AC-027)."""
        return {
            "per_fill_markout": self.per_fill_markout,
            "matched_opportunity_markout": self.matched_opportunity_markout,
            "exposure_normalized_adverse_selection": self.exposure_normalized_adverse_selection,
            "fill_count": self.fill_count,
            "abstention_count": self.abstention_count,
            "capital_at_risk": self.capital_at_risk,
        }


def _venue_mid(obs: StrategyObservation) -> float | None:
    """The venue mid ``(bid + ask) / 2`` when the book has both sides, else ``None`` (a stale book)."""
    if obs.bid is None or obs.ask is None:
        return None
    return (obs.bid + obs.ask) / 2.0


def _horizon_indices(mids: list[float | None]) -> list[int | None]:
    """For each observation, the index of the NEXT venue CHANGE — the event-time markout horizon.

    ``horizon[i]`` is the smallest ``j > i`` whose venue mid is present AND differs from ``mid[i]`` (a
    genuine venue change on the step-function tape), or ``None`` when the book is stale at ``i`` or no
    future change exists. This is EVENT-TIME, never a fixed wall-clock offset (REQ-113)."""
    horizons: list[int | None] = []
    for i, now in enumerate(mids):
        idx: int | None = None
        if now is not None:
            for j in range(i + 1, len(mids)):
                if mids[j] is not None and mids[j] != now:
                    idx = j
                    break
        horizons.append(idx)
    return horizons


def venue_future_mid_series(
    observations: tuple[StrategyObservation, ...],
) -> tuple[float | None, ...]:
    """The event-time next-venue-change mid for each observation (VENUE-derived reference; REQ-113).

    ``None`` where the book is stale or no future venue change exists. This is the ONLY reference the
    markout is scored against — the venue's own future price, never the FV the guard consumes.
    """
    mids = [_venue_mid(o) for o in observations]
    horizons = _horizon_indices(mids)
    return tuple(mids[h] if h is not None else None for h in horizons)


def _markout_bps(*, leg_role: str, quote_price: float, venue_now: float, venue_future: float) -> int:
    """Signed venue-referenced markout in bps — a bid gains when the venue rises, an ask when it falls.

    Mirrors ``veridex.maker.markout.forward_markout_bps`` in shape but is re-implemented here so this
    TEST-SIDE harness never imports the maker tier. The reference is ``venue_future`` (the venue's OWN
    future mid), never the FV.
    """
    sign = 1 if leg_role == "bid" else -1
    return round(sign * (venue_future - quote_price) / venue_now * 1e4)


def _candidate_fills(result: ReplayResult, *, reference: str) -> tuple[CandidateFill, ...]:
    """Every resting quote leg with a scorable event-time horizon, scored against the venue future mid.

    A resting leg (``leg_role`` in bid/ask with a price) at observation ``i`` is a candidate fill when a
    future venue change exists. ``reference`` MUST be ``"venue"``: an FV reference is circular and fails
    closed (REQ-113/RED-26). Decisions are folded 1:1 over the observation stream in ``replay_arm``, so
    ``result.decisions[i]`` is the decision on ``result.observations[i]``.
    """
    if reference != "venue":
        raise MarkoutReferenceError(
            f"markout reference must be venue-derived (the venue's own future mid), not {reference!r}: "
            "scoring the FV-driven guard against the FV it consumes is circular self-validation "
            "(REQ-113/RED-26)"
        )
    observations = result.observations
    mids = [_venue_mid(o) for o in observations]
    horizons = _horizon_indices(mids)
    fills: list[CandidateFill] = []
    for i, decision in enumerate(result.decisions):
        now = mids[i]
        h = horizons[i]
        if now is None or now == 0.0 or h is None:
            continue
        future = mids[h]
        if future is None:
            continue
        for leg in decision.intent_plan:
            if leg.leg_role not in _QUOTE_LEG_ROLES or leg.price is None:
                continue
            fills.append(
                CandidateFill(
                    observation_index=i,
                    horizon_index=h,
                    leg_role=leg.leg_role,
                    quote_price=leg.price,
                    venue_now=now,
                    venue_future_mid=future,
                    markout_bps=_markout_bps(
                        leg_role=leg.leg_role,
                        quote_price=leg.price,
                        venue_now=now,
                        venue_future=future,
                    ),
                )
            )
    return tuple(fills)


def _capital_at_risk(result: ReplayResult) -> float:
    """Quote-side exposure — the sum of resting-leg quote prices (unit notional; R4-B proposes no size).

    R4-B v0 carries NO size field (REQ-057), so the per-leg notional is the quote price itself; capital
    at risk is the total price rested across the arm's quote legs.
    """
    total = 0.0
    for decision in result.decisions:
        for leg in decision.intent_plan:
            if leg.leg_role in _QUOTE_LEG_ROLES and leg.price is not None:
                total += leg.price
    return total


def _abstention_count(result: ReplayResult) -> int:
    """Count of ``NO_QUOTE`` abstentions — the guard's fail-closed abstentions (NOT 'fewer trades')."""
    return sum(1 for d in result.decisions if d.kind == "NO_QUOTE")


def _matched_opportunity_deltas(
    baseline_fills: tuple[CandidateFill, ...], guarded_fills: tuple[CandidateFill, ...]
) -> list[int]:
    """Paired guarded-minus-baseline markout over the SAME eligible opportunities (REQ-112).

    Keyed on ``(observation_index, leg_role)`` — the SAME venue opportunity — so the delta compares the
    two arms ONLY where BOTH rested a leg. This strips the selection bias the spec names (arm B looking
    better merely because it traded LESS): an opportunity arm B abstained on is absent from BOTH sides.
    """
    a = {(f.observation_index, f.leg_role): f.markout_bps for f in baseline_fills}
    b = {(f.observation_index, f.leg_role): f.markout_bps for f in guarded_fills}
    return [b[key] - a[key] for key in sorted(a.keys() & b.keys())]


def matched_opportunity_report(
    baseline: ReplayResult,
    guarded: ReplayResult,
    *,
    reference: str = MARKOUT_REFERENCE,
) -> MatchedOpportunityReport:
    """Score the guarded arm vs the baseline into the SIX mandatory metrics (REQ-112/113/AC-027).

    All six metrics are computed and returned TOGETHER — there is no partial-report path. Every markout
    is scored against the venue's OWN future mid at the next venue change (event-time); ``reference``
    other than ``"venue"`` (e.g. ``"fv"``) FAILS CLOSED via :class:`MarkoutReferenceError`. The default
    ``reference`` is the pinned :data:`MARKOUT_REFERENCE` — the RED-26 mutation flips that constant to
    ``"fv"``, which routes the default call through the fail-closed guard.
    """
    guarded_fills = _candidate_fills(guarded, reference=reference)
    baseline_fills = _candidate_fills(baseline, reference=reference)

    per_fill = (
        sum(f.markout_bps for f in guarded_fills) / len(guarded_fills)
        if guarded_fills
        else 0.0
    )
    deltas = _matched_opportunity_deltas(baseline_fills, guarded_fills)
    matched = sum(deltas) / len(deltas) if deltas else 0.0

    capital = _capital_at_risk(guarded)
    adverse_mass = float(sum(-f.markout_bps for f in guarded_fills if f.markout_bps < 0))
    adverse_norm = adverse_mass / capital if capital > 0.0 else 0.0

    return MatchedOpportunityReport(
        per_fill_markout=per_fill,
        matched_opportunity_markout=matched,
        exposure_normalized_adverse_selection=adverse_norm,
        fill_count=len(guarded_fills),
        abstention_count=_abstention_count(guarded),
        capital_at_risk=capital,
        markout_reference="venue",
        matched_opportunity_count=len(deltas),
        fills=guarded_fills,
    )


# --- E6-T4: forbidden-comparison guard — the ONLY permitted A/B conclusion shape -------------
# REQ-114 names three comparisons that are FORBIDDEN as a benefit verdict on their own:
#   (1) total-PnL-alone, (2) fill-count-alone ("fewer trades ≠ better"), (3) per-fill-markout-alone.
# An arm that merely trades LESS with a lower total loss is NOT "better" — that is selection bias, not a
# risk edge. The single conclusion the harness will ever yield is a matched-opportunity risk-edge
# HYPOTHESIS, pending Gate B: hypothesis-only, evidence-gated, never a promotion. :class:`AblationConclusion`
# has NO benefit/better/winner field, so by construction a favorable single metric can never travel as a
# verdict. The three forbidden single-metric deltas ARE recorded on the conclusion (an honest packet is
# transparent about what was observed) but the verdict — :meth:`AblationConclusion.infers_benefit` — is
# structurally blind to them. This is offline diagnostics over an already-replayed arm pair; no
# ranked/research/maker import (the ``forbidden_import_hits`` guard still holds).

# The three FORBIDDEN-alone comparison names (REQ-114) — each is diagnostic, none is a verdict.
FORBIDDEN_SINGLE_METRICS = frozenset({"total_pnl", "fill_count", "per_fill_markout"})

# The ONLY conclusion shape the harness permits (REQ-114). It is a HYPOTHESIS over MATCHED opportunities,
# explicitly PENDING Gate B — it contains no "benefit"/"better"/"winner" term, so no verdict can hide in
# the label. The RED-25 mutation (a benefit flag keyed on lower total loss / fewer trades) can only live
# in ``infers_benefit`` below; this constant stays hypothesis-only regardless.
PERMITTED_CONCLUSION_SHAPE = "risk-edge hypothesis on matched opportunities, pending Gate B"


@dataclass(frozen=True)
class ArmSingleMetrics:
    """One arm's three FORBIDDEN-alone single metrics (REQ-114) — none is a benefit verdict on its own.

    ``total_markout_bps`` is the arm's total venue-referenced markout (the "total PnL" comparison),
    ``fill_count`` its candidate-fill count ("fewer trades ≠ better"), ``per_fill_markout`` its mean
    per-fill markout. Grouped here so a conclusion can RECORD the differentials transparently while its
    verdict stays blind to them — a lower total loss / fewer trades / better per-fill never infers benefit.
    """

    total_markout_bps: float
    fill_count: int
    per_fill_markout: float


@dataclass(frozen=True)
class AblationConclusion:
    """The ONLY permitted A/B conclusion: a matched-opportunity risk-edge HYPOTHESIS, pending Gate B.

    There is deliberately NO ``benefit`` / ``better`` / ``winner`` field: a favorable single metric can
    never travel as a verdict (REQ-114). The three forbidden single-metric deltas (``total_markout_delta``,
    ``fill_count_delta``, ``per_fill_markout_delta``) are recorded so the packet is honest about what was
    observed, but :meth:`infers_benefit` is structurally blind to them — the conclusion is fed ONLY by the
    matched-opportunity delta (the honest paired A-vs-B markout over the SAME eligible opportunities) and
    stays a hypothesis pending Gate B. ``hypothesis_only`` / ``gate_b_pending`` are invariants of the shape.
    """

    matched_opportunity_markout: float
    total_markout_delta: float
    fill_count_delta: int
    per_fill_markout_delta: float
    shape: str = PERMITTED_CONCLUSION_SHAPE
    hypothesis_only: bool = True
    gate_b_pending: bool = True

    def infers_benefit(self) -> bool:
        """Whether this conclusion infers benefit from a single favorable metric — ALWAYS ``False`` (REQ-114).

        A lower total loss, fewer trades, or a better per-fill markout can NEVER become a benefit verdict:
        the only conclusion is the matched-opportunity hypothesis pending Gate B. The recorded single-metric
        deltas are intentionally NOT consulted here — the RED-25 mutation keys a benefit flag on one of them
        (e.g. ``self.total_markout_delta > 0``), which this honest implementation refuses to do.
        """
        return False


def arm_single_metrics(result: ReplayResult) -> ArmSingleMetrics:
    """The arm's three forbidden-alone single metrics from its OWN venue-referenced candidate fills.

    Total markout (sum), fill count, and mean per-fill markout — each a FORBIDDEN-alone comparison input
    (REQ-114). Scored against ``MARKOUT_REFERENCE`` (the venue's own future mid), never the FV the guard
    consumes, so the RED-26 fail-closed guard still applies uniformly.
    """
    fills = _candidate_fills(result, reference=MARKOUT_REFERENCE)
    total = float(sum(f.markout_bps for f in fills))
    per_fill = total / len(fills) if fills else 0.0
    return ArmSingleMetrics(
        total_markout_bps=total,
        fill_count=len(fills),
        per_fill_markout=per_fill,
    )


def ablation_conclusion(
    report: MatchedOpportunityReport,
    baseline: ArmSingleMetrics,
    guarded: ArmSingleMetrics,
) -> AblationConclusion:
    """Reduce a guarded-vs-baseline six-metric report to the ONLY permitted conclusion (REQ-114).

    The forbidden single-metric differentials (total PnL, fill count, per-fill markout) are recorded on the
    conclusion for an honest packet, but they are DELIBERATELY not consulted for the verdict: the conclusion
    is the matched-opportunity risk-edge HYPOTHESIS pending Gate B — the honest paired delta over the SAME
    eligible opportunities — and nothing else. A benefit/"better" verdict from any single favorable metric
    (a lower total loss, fewer trades, a better per-fill markout) is forbidden by the shape itself.
    """
    return AblationConclusion(
        matched_opportunity_markout=report.matched_opportunity_markout,
        total_markout_delta=guarded.total_markout_bps - baseline.total_markout_bps,
        fill_count_delta=guarded.fill_count - baseline.fill_count,
        per_fill_markout_delta=guarded.per_fill_markout - baseline.per_fill_markout,
    )


# --- E6-T5: print / reach / label honesty — a print is a diagnostic, never a trigger / fill --
# REQ-115 / HON-002 / HON-004 / HON-005 / REQ-026/027/052/053/054. Gate B pins the execution-evidence
# CEILING: a third-party trade print is an OBSERVED_MARKET_PRINT — an OBSERVATION of the market, NEVER
# our own fill / PnL / capacity / arrival-order (REQ-027). This block makes three honesty seams
# STRUCTURAL (not a runtime check that could be bypassed):
#   (1) a print carries its OWN third-party-print timing, kept SEPARATE from our book-arrival timing —
#       conflating them would let a print masquerade as our own book event;
#   (2) a raw reach % / a single print is structurally NOT a member of the closed REQ-080 venue-book
#       trigger vocabulary (:data:`TRIGGER_REASON_CODES`, sourced from ``contracts.ReasonCode``) and
#       NEVER becomes our fill or feeds PnL;
#   (3) counterfactual capacity is a RECOMPUTED CEILING (an upper bound from observed prints), never
#       our realized fill / PnL / capacity; and a reach scored against a forbidden universal null
#       (``0.5`` / ``p/b``) can never assert edge (REQ-054 / HON-004).
# Private practitioner material (the privacy-locked quote/level-age anecdote) stays INTERNAL — it is
# never a field on the public diagnostic here (HON-005). TEST-SIDE, no ranked/research/maker import
# (``forbidden_import_hits`` still holds — nothing below imports a ranked lane).

# The Gate B execution-evidence CEILING label EVERY third-party trade print carries (REQ-026): an
# OBSERVATION of the market, never OWN_RECONCILED_FILL — which is structurally unavailable to an
# archive/replay harness (REQ-052) and named here only so a test can assert a print never wears it.
OBSERVED_MARKET_PRINT = "OBSERVED_MARKET_PRINT"
OWN_RECONCILED_FILL = "OWN_RECONCILED_FILL"

# The closed REQ-080 venue-book EVENT trigger vocabulary — the ONLY reason codes that pull a live
# quote. Sourced from the ``contracts.ReasonCode`` Literal so it can never silently drift from the
# core; a reach % / print label is structurally NOT a member.
TRIGGER_REASON_CODES = frozenset(get_args(ReasonCode))

# Reach baselines FORBIDDEN as a universal null (REQ-054 / HON-004): a raw ``0.5`` and the naive
# price/book (``p/b``) reach are NOT reviewed nulls — a reach scored against them can never assert
# edge. This is a SCOPED negative (exactly these two nulls), not a universal ban on reach.
FORBIDDEN_REACH_NULLS = frozenset({"0.5", "p/b"})


class ForbiddenReachNullError(ValueError):
    """A reach measured against a forbidden universal null (``0.5`` / ``p/b``) — REQ-054 / HON-004.

    Fail-closed: a raw ``0.5`` or a price/book reach is not a reviewed null, so a reach scored against
    it can never assert edge. The harness REFUSES the baseline rather than silently minting a spurious
    edge — while still permitting reach as a diagnostic against a reviewed null (the scoped-negative,
    HON-004).
    """


@dataclass(frozen=True)
class ObservedMarketPrint:
    """A third-party trade print — an ``OBSERVED_MARKET_PRINT`` diagnostic (Gate B ceiling; REQ-026/027).

    A print is an OBSERVATION of the market's own activity: NEVER our fill, PnL, capacity, or
    arrival-order (REQ-027). ``print_recv_ts`` (when WE observed the third-party print) is kept SEPARATE
    from ``book_arrival_ts`` (when our OWN venue book observation arrived) — conflating the two timings
    would let a third-party print masquerade as our book event. ``reach_fraction`` is a raw diagnostic %
    only: it is neither a trigger (:func:`print_derived_trigger`) nor an edge claim. ``label`` is fixed
    to :data:`OBSERVED_MARKET_PRINT`; the harness never mints :data:`OWN_RECONCILED_FILL` for a print.
    No private practitioner field (quote/level-age anecdote) is carried — that material stays internal
    (HON-005).
    """

    price: float
    size: float
    print_recv_ts: int
    book_arrival_ts: int
    reach_fraction: float
    label: str = OBSERVED_MARKET_PRINT

    def is_our_fill(self) -> bool:
        """A print is NEVER our fill (REQ-027/052) — structurally ``False``."""
        return False

    def pnl_contribution(self) -> float:
        """A print NEVER feeds PnL (REQ-027) — structurally ``0.0``."""
        return 0.0


def print_derived_trigger(mark: ObservedMarketPrint) -> ReasonCode | None:
    """The live-quote trigger a third-party print / its reach % contributes — ALWAYS ``None`` (REQ-115).

    The live trigger surface is the closed REQ-080 venue-book EVENT set (:data:`TRIGGER_REASON_CODES`);
    a print / reach % is an ``OBSERVED_MARKET_PRINT`` diagnostic and is structurally NOT a member.
    Returning ``None`` keeps a raw reach %/print out of the quote decision entirely. The RED-28 mutation
    (return a real ``ReasonCode`` here, e.g. keyed on ``mark.reach_fraction``) routes a print into the
    trigger set and ``test_raw_reach_or_print_cannot_become_trigger_or_fill`` goes red.
    """
    return None


@dataclass(frozen=True)
class CounterfactualCapacityCeiling:
    """A RECOMPUTED counterfactual capacity CEILING from observed prints — NOT our fill / PnL / capacity.

    ``ceiling_size`` is an UPPER BOUND recomputed from the observed third-party print sizes (the honest
    no-queue / no-own-fill ceiling — the same evidence ceiling E6-T3 pins for markout). ``is_ceiling`` /
    ``is_our_capacity`` make the labeling structural: this is never our realized fill, PnL, or capacity
    (REQ-027/115). ``label`` stays :data:`OBSERVED_MARKET_PRINT` — the ceiling is derived from
    observations, not from anything we executed.
    """

    ceiling_size: float
    is_ceiling: bool = True
    is_our_capacity: bool = False
    label: str = OBSERVED_MARKET_PRINT


def counterfactual_capacity_ceiling(
    prints: tuple[ObservedMarketPrint, ...],
) -> CounterfactualCapacityCeiling:
    """Recompute the counterfactual capacity CEILING from observed prints (REQ-115 / REQ-027).

    The ceiling is the summed observed print size — an upper bound on what COULD have been reached, not
    a claim about what we filled. It is recomputed from the observations every call (producer metadata
    is never trusted; REQ-052) and carries ``is_our_capacity=False`` so it can never be read as our
    realized capacity.
    """
    return CounterfactualCapacityCeiling(ceiling_size=float(sum(p.size for p in prints)))


def reviewed_reach_baseline(baseline: str) -> str:
    """Validate a reach baseline is a REVIEWED null, not a forbidden UNIVERSAL one (REQ-054 / HON-004).

    ``0.5`` and the naive price/book (``p/b``) reach are FORBIDDEN as a reach baseline — a reach scored
    against them can never assert edge — so they fail closed via :class:`ForbiddenReachNullError`. A
    reviewed baseline is returned unchanged. This is a SCOPED negative (exactly those two nulls), never a
    universal ban on reach as a diagnostic (HON-004).
    """
    if baseline in FORBIDDEN_REACH_NULLS:
        raise ForbiddenReachNullError(
            f"reach baseline {baseline!r} is a forbidden universal null (REQ-054 / HON-004): a raw 0.5 "
            "or a price/book (p/b) reach is not a reviewed null and cannot assert edge"
        )
    return baseline


# --- E6-T6: run-receipt labels/hashes + relabel-fails-closed + historical reproduction --------
# REQ-116 (run-receipt provenance + labels) / REQ-043(H42) (evidence-class gating: Gate-B OPEN/STALE ⇒
# EXPERIMENTAL_DUST) / AC-029 / AC-030. A run receipt is the honest provenance packet one replayed arm
# emits: it PINS the full id/hash chain (strategy identity, ``config_hash``, per-observation hashes, the
# linked state-hash chain, the per-decision ids, the canonical decisions digest) plus the Gate-B evidence
# revision consumed and the four mandatory honesty labels. Two trust seams are STRUCTURAL here:
#   (1) the evidence class is PINNED to EXPERIMENTAL_DUST — R4-B proves SAFETY, not alpha, so there is NO
#       code path that promotes it to EVIDENCE_GATED / PROMOTED (promotion is a Gate B concern, out of
#       R4-B scope). Under Gate-B OPEN/STALE an untrusted request-metadata relabel (asking for PROMOTED /
#       EVIDENCE_GATED) has ZERO effect — the receipt FAILS CLOSED and stays EXPERIMENTAL_DUST;
#   (2) the receipt pins the ORIGINAL ``config_hash``, so a later config revision (a new ``config_hash``)
#       never rewrites history: the historical tape replays byte-identically under its originally-pinned
#       config. The four labels are the pinned production honesty constants VERBATIM (imported, not
#       re-declared), so the receipt can never drift from the pinned run labels.
# TEST-SIDE, no ranked/research/maker import (``forbidden_import_hits`` still holds — nothing here imports
# a ranked lane; the labels come from the pure ``veridex.mm_strategy.contracts`` honesty surface).

# The two promotion evidence classes an R4-B receipt can NEVER wear (REQ-043(H42)/AC-029). Promotion is a
# Gate B concern out of R4-B scope; naming them lets a test assert the fail-closed pin refuses them.
PROMOTED_EVIDENCE_CLASSES = frozenset({"EVIDENCE_GATED", "PROMOTED"})


@dataclass(frozen=True)
class RunReceipt:
    """The provenance packet one replayed arm emits — the pinned id/hash chain + honesty labels (REQ-116).

    Pins the strategy identity (``strategy_id`` / ``strategy_revision``), the ``config_hash`` the run
    executed under, every per-observation hash, the linked ``state_hash_chain`` (the folded prior→next
    state-hash lineage) + its ``terminal_state_hash``, the per-decision ``decision_ids``, the canonical
    ``decisions_digest``, and the Gate-B status + evidence revision consumed. ``evidence_class`` is PINNED
    to :data:`EVIDENCE_CLASS` (``EXPERIMENTAL_DUST``; REQ-043(H42)); the four honesty labels are the pinned
    production constants verbatim. Frozen + tuple-valued, so two receipts minted from the SAME replay
    under the SAME pinned config compare byte-identically (AC-030).
    """

    strategy_id: str
    strategy_revision: str
    config_hash: str
    observation_hashes: tuple[str, ...]
    decision_ids: tuple[str, ...]
    state_hash_chain: tuple[str, ...]
    terminal_state_hash: str
    decisions_digest: str
    gate_b_status: str
    gate_b_evidence_revision: str
    evidence_class: str
    run_label: str
    calibration_label: str
    edge_label: str

    def labels(self) -> dict[str, str]:
        """The four mandatory honesty labels as ONE mapping (REQ-116) — evidence class pinned to dust."""
        return {
            "evidence_class": self.evidence_class,
            "run_label": self.run_label,
            "calibration_label": self.calibration_label,
            "edge_label": self.edge_label,
        }


def _state_hash_chain(result: ReplayResult) -> tuple[str, ...]:
    """The folded prior→next state-hash lineage of an arm's decision stream (the linked chain; REQ-116).

    ``(decisions[0].prior_state_hash, decisions[0].next_state_hash, decisions[1].next_state_hash, …)`` —
    each decision's ``prior_state_hash`` equals the previous decision's ``next_state_hash`` by
    construction of the fold, so the tuple is the linked state-hash chain the receipt pins; the last
    element is the terminal ``state_hash``. Empty when the stream is empty.
    """
    decisions = result.decisions
    if not decisions:
        return ()
    chain = [decisions[0].prior_state_hash]
    chain.extend(d.next_state_hash for d in decisions)
    return tuple(chain)


def mint_run_receipt(
    result: ReplayResult,
    config: StrategyConfig,
    *,
    gate_b_status: str,
    gate_b_evidence_revision: str,
    requested_evidence_class: str | None = None,
) -> RunReceipt:
    """Mint the pinned provenance receipt for one replayed arm (REQ-116/043(H42)/AC-029/AC-030).

    Every field is a pure function of the already-replayed ``result`` + the run's pinned ``config`` + the
    Gate-B evidence revision consumed, so re-minting from the same replay under the same pinned config
    reproduces the receipt byte-identically (AC-030). ``requested_evidence_class`` is UNTRUSTED request
    metadata: the evidence class is PINNED to :data:`EVIDENCE_CLASS` (``EXPERIMENTAL_DUST``) and the
    request has ZERO effect — R4-B has no promotion path, so under Gate-B OPEN/STALE an asked-for
    ``PROMOTED`` / ``EVIDENCE_GATED`` relabel FAILS CLOSED (AC-029). The four honesty labels are the pinned
    production constants verbatim (no re-declaration → no drift). The RED-29 mutation honors
    ``requested_evidence_class`` here, routing the relabel through so ``evidence_class`` becomes the
    promotion class and ``test_gate_b_open_metadata_relabel_stays_experimental_dust`` goes red.
    """
    # PINNED — the requested relabel is untrusted request metadata with ZERO effect (AC-029). There is no
    # branch that assigns a promotion class: promotion is a Gate B concern, out of R4-B scope.
    evidence_class = EVIDENCE_CLASS
    return RunReceipt(
        strategy_id=config.strategy_id,
        strategy_revision=STRATEGY_REVISION,
        config_hash=config.config_hash(),
        observation_hashes=result.observation_hashes,
        decision_ids=tuple(d.decision_id for d in result.decisions),
        state_hash_chain=_state_hash_chain(result),
        terminal_state_hash=result.state_hash,
        decisions_digest=result.decisions_digest,
        gate_b_status=gate_b_status,
        gate_b_evidence_revision=gate_b_evidence_revision,
        evidence_class=evidence_class,
        run_label=RUN_LABEL,
        calibration_label=CALIBRATION_LABEL,
        edge_label=EDGE_LABEL,
    )
