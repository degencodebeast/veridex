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
from typing import Any

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
    GuardFairValue,
    InventoryProjection,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
    StreamIdentity,
)
from veridex.mm_strategy.core import decide

__all__ = [
    "FIXTURES_DIR",
    "HARNESS_MODULE_PATH",
    "ArmConfigs",
    "LoadedTape",
    "ReplayResult",
    "arm_configs",
    "config_field_diff",
    "forbidden_import_hits",
    "load_base_config_overrides",
    "load_tape",
    "neutralize_guard",
    "replay_arm",
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
) -> StrategyObservation:
    """One healthy per-tick observation — arm-IDENTICAL venue/match-state facts; only ``guard_fv`` is
    arm-dependent. Mirrors the E3-T4 honest builder so ``run_cadence``'s field-for-field authentication
    passes. Every ``recv_ts`` is ``<= as_of_ts`` (REQ-022 guard)."""
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
        bid=0.49,
        ask=0.51,
        bid_size=100.0,
        ask_size=120.0,
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
    cadence-assigned sequence + projected guard leg onto arm-identical venue facts."""
    as_of_ts = int(raw["as_of_ts"])

    def build(
        observation_sequence: int, guard_fv: GuardFairValue | None
    ) -> StrategyObservation:
        return _observation(
            observation_sequence=observation_sequence,
            guard_fv=guard_fv,
            as_of_ts=as_of_ts,
            identity=identity,
            venue_market_ref=venue_market_ref,
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
