"""Read-only Gamma market-status authority guards (MM-R4-B — REQ-027 / AC-053).

Offline by construction: every test injects a FAKE fetch seam returning canned Gamma
market metadata, so nothing here makes a live/credentialed Gamma call. Three invariants are
pinned:

1. the total, disjoint ``active``/``closed`` → :data:`MarketStatus` map (``closed`` WINS);
2. the fail-closed contract — any fetch/parse failure or missing/malformed flag maps to
   ``UNKNOWN`` with the ``(None, None)`` sentinels (never a fabricated ``ACTIVE``);
3. a static/AST scan proving the module imports NOTHING that submits/cancels/signs.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest
from pydantic import ValidationError

import veridex.venues.market_status as market_status
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    RestingOrderView,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import decide
from veridex.venues.market_status import GammaMarketStatusAuthority


def _authority(
    metadata_by_ref: dict[str, Any],
    *,
    epoch: int = 7,
    recv_ts: int = 1000,
) -> GammaMarketStatusAuthority:
    """Build an authority whose fetch seam is a pure lookup over canned metadata (offline).

    An unknown ``venue_market_ref`` raises ``KeyError`` inside the fetch — exercising the
    fetch-failure → UNKNOWN path without any network.
    """

    def fetch(venue_market_ref: str) -> Any:
        return metadata_by_ref[venue_market_ref]

    return GammaMarketStatusAuthority(
        fetch=fetch, clock=lambda: recv_ts, epoch=epoch
    )


def test_gamma_active_closed_maps_total_and_disjoint() -> None:
    # All four boolean cells of (active, closed) resolve to exactly one deterministic status,
    # and a CLOSED market wins even when it is also flagged active (REQ-027; closed WINS).
    cases: dict[tuple[bool, bool], str] = {
        (True, False): "ACTIVE",
        (True, True): "CLOSED",  # closed wins over active
        (False, True): "CLOSED",  # closed wins (¬active does not matter once closed)
        (False, False): "HALTED",
    }
    for (active, closed), expected in cases.items():
        # conditionId EQUALS the requested ref: the honest same-market case (identity binds).
        metadata = {"active": active, "closed": closed, "conditionId": "ref"}
        authority = _authority({"ref": metadata}, epoch=7, recv_ts=1000)

        status, recv_ts, epoch = authority.read("ref")

        assert status == expected, f"(active={active}, closed={closed}) → {status!r}"
        # A definite (non-UNKNOWN) status carries non-None sentinels: recv_ts is the read
        # clock, epoch the authority generation.
        assert recv_ts == 1000
        assert epoch == 7


def test_read_failure_is_unknown_with_none_sentinels() -> None:
    # Every failure/missing/malformed shape maps to (UNKNOWN, None, None) — the fail-closed
    # honesty rule: the authority never fabricates a definite status it cannot prove.
    # (a) fetch failure (ref absent from the canned map → KeyError inside the seam).
    authority = _authority({}, epoch=7, recv_ts=1000)
    assert authority.read("missing") == ("UNKNOWN", None, None)

    # (b) missing active/closed flags.
    only_active = _authority({"ref": {"active": True}}, epoch=7)
    assert only_active.read("ref") == ("UNKNOWN", None, None)
    only_closed = _authority({"ref": {"closed": False}}, epoch=7)
    assert only_closed.read("ref") == ("UNKNOWN", None, None)

    # (c) non-boolean flags (Gamma booleans are real JSON bools; a string/int is malformed).
    string_flags = _authority(
        {"ref": {"active": "true", "closed": "false"}}, epoch=7
    )
    assert string_flags.read("ref") == ("UNKNOWN", None, None)

    # (d) a non-mapping fetch result (e.g. an empty list) is malformed, not a market object.
    non_mapping = _authority({"ref": []}, epoch=7)
    assert non_mapping.read("ref") == ("UNKNOWN", None, None)


def test_live_foreign_conditionid_is_unknown() -> None:
    # Gate #2 MAJOR-3 (LIVE): the authority must BIND the response identity to the requested
    # reference. A fetch for 0xEXPECTED returning a HEALTHY, otherwise-ACTIVE market whose OWN
    # identity is 0xFOREIGN (conditionId=0xFOREIGN, active ∧ ¬closed) must NOT be accepted as this
    # market's status — the returned object describes a DIFFERENT market. Unbound, this yields a
    # foreign market's ACTIVE; bound, an identity mismatch fails closed to (UNKNOWN, None, None).
    foreign = _authority(
        {"0xEXPECTED": {"active": True, "closed": False, "conditionId": "0xFOREIGN"}},
        epoch=7,
        recv_ts=1000,
    )
    assert foreign.read("0xEXPECTED") == ("UNKNOWN", None, None)

    # A missing conditionId cannot be proven to match the request either → UNKNOWN (fail closed),
    # never a definite status inferred from an unidentifiable object.
    no_identity = _authority(
        {"0xEXPECTED": {"active": True, "closed": False}}, epoch=7, recv_ts=1000
    )
    assert no_identity.read("0xEXPECTED") == ("UNKNOWN", None, None)

    # The honest same-market case is UNCHANGED: a fetch whose conditionId EQUALS the requested ref
    # reads its definite status back with the non-None sentinels.
    same_market = _authority(
        {"0xEXPECTED": {"active": True, "closed": False, "conditionId": "0xEXPECTED"}},
        epoch=7,
        recv_ts=1000,
    )
    assert same_market.read("0xEXPECTED") == ("ACTIVE", 1000, 7)


def test_module_imports_no_write_or_signer() -> None:
    # Static/AST proof the read-only authority pulls in NOTHING that could submit, cancel, or
    # sign: not veridex.venues.base / veridex.venues.sx_bet, and no submit_order/cancel_order
    # /private-key/EIP-712 symbol. (The mutation adds a sx_bet import → this test fails.)
    source = inspect.getsource(market_status)
    tree = ast.parse(source)

    imported_paths: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_paths.add(node.module)
            for alias in node.names:
                identifiers.add(alias.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    forbidden_paths = {"veridex.venues.base", "veridex.venues.sx_bet"}
    offending_imports = {
        path
        for path in imported_paths
        for bad in forbidden_paths
        if path == bad or path.startswith(bad + ".")
    }
    assert not offending_imports, f"read-only module imports a write/venue module: {offending_imports}"

    forbidden_symbols = {"submit_order", "cancel_order"}
    offending_symbols = identifiers & forbidden_symbols
    assert not offending_symbols, f"read-only module references a write symbol: {offending_symbols}"

    # Signer markers are checked against CODE symbols (imported paths + identifiers) ONLY — never
    # raw prose — so a docstring naming what the module refuses to import can't trip the bar (the
    # same code-only discipline as the pure-tier import audit).
    code_symbols = " ".join(imported_paths | identifiers).lower()
    for marker in ("private_key", "eip712", "eip_712"):
        assert marker not in code_symbols, f"read-only module references a signer marker: {marker!r}"


# --- Core market-active gate: freshness / future-dating / regression (REQ-021/026; E3-T6) ---
# These pin the CORE's own status re-check (the assembler already maps missing/failed/regressed to
# UNKNOWN — the core independently re-checks age AND regression against its durable state watermark).
# The window is TWO-sided: the lower bound (future-dating) is a construction guard (REQ-022 — an
# unconstructible input is outside the transition table); the upper bound (staleness) + the epoch/recv
# regression are core gates that treat any non-current non-UNKNOWN status as UNKNOWN (fail closed).


def _config(**overrides: object) -> StrategyConfig:
    """Baseline (guard-off) config; ``market_status_max_age_ms`` is the default 30_000 ms."""
    return StrategyConfig(guard_enabled=False, **overrides)  # type: ignore[arg-type]


def _resting_order() -> RestingOrderView:
    """One resting open order — the exposure a HALTED/CLOSED market's cancel plan would target."""
    return RestingOrderView(client_order_id="c-1", side="YES", price=0.49, size=10.0)


def _obs(
    *,
    market_status: str = "ACTIVE",
    market_status_epoch: int | None = 5,
    market_status_recv_ts: int | None = None,
    as_of_ts: int = 100_000,
    observation_sequence: int = 2,
    book_source_epoch: int = 1,
    net_position: float = 0.0,
    resting: tuple[RestingOrderView, ...] = (),
) -> StrategyObservation:
    """A healthy guard-off observation whose ONLY interesting knobs are the market-status triple +
    the ``as_of`` clock. Every other ``recv_ts`` is derived ≤ ``as_of_ts`` so construction only trips
    the REQ-022 future-dating guard when a status ``recv_ts`` override is placed deliberately ahead."""
    recv = as_of_ts - 10
    if market_status == "UNKNOWN":
        status_recv: int | None = None
        status_epoch: int | None = None
    else:
        status_recv = recv if market_status_recv_ts is None else market_status_recv_ts
        status_epoch = market_status_epoch
    return StrategyObservation(
        fixture_id=1,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id="TOKEN-YES",
        venue_market_ref="0xmarket",
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
        guard_fv=None,
        market_status=market_status,  # type: ignore[arg-type]
        market_status_recv_ts=status_recv,
        market_status_epoch=status_epoch,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=net_position,
            resting=resting,
            projection_as_of_ts=as_of_ts,
            fresh=True,
        ),
        as_of_ts=as_of_ts,
    )


def _warm_state(
    *,
    last_market_status_epoch: int | None = 1,
    last_market_status_recv_ts: int | None = 1,
    last_as_of_ts: int = 99_000,
    last_observation_sequence: int = 1,
    last_book_source_epoch: int = 1,
) -> StrategyState:
    """A mid-stream state with WARM references (smoother seeded + both rolling refs past
    ``ref_min_samples``) so an ACTIVE in-window frame reaches row H and is placement-eligible. The
    status watermark defaults low so a normal frame is neither over-age-relative nor regressed."""
    return StrategyState(
        last_observation_sequence=last_observation_sequence,
        last_book_source_epoch=last_book_source_epoch,
        last_as_of_ts=last_as_of_ts,
        last_market_status_epoch=last_market_status_epoch,
        last_market_status_recv_ts=last_market_status_recv_ts,
        smoother_mid=0.5,
        smoother_mid_ts=last_as_of_ts,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


def test_future_dated_active_is_construction_error() -> None:
    # A status stamped AFTER the decision clock (recv_ts > as_of_ts) is unconstructible: it never
    # becomes an observation, so it is outside the transition table entirely (REQ-022/026; RED-41).
    with pytest.raises(ValidationError) as exc:
        _obs(market_status="ACTIVE", market_status_recv_ts=100_010, as_of_ts=100_000)
    message = str(exc.value)
    assert "market_status_recv_ts" in message
    assert "future-dated" in message


def test_stale_active_treated_as_unknown() -> None:
    # A validly-sourced ACTIVE whose recv_ts is older than market_status_max_age_ms is temporally
    # invalid: provenance authenticates WHO said ACTIVE, not that it is STILL current (AC-043/RED-38).
    config = _config()  # market_status_max_age_ms default 30_000
    state = _warm_state(last_market_status_epoch=1, last_market_status_recv_ts=1)
    obs = _obs(
        market_status="ACTIVE",
        market_status_epoch=5,
        market_status_recv_ts=100_000 - 40_000,  # age 40_000 > 30_000 → stale
        as_of_ts=100_000,
    )
    decision, next_state = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("market_status_unknown",)
    # A stale ACTIVE is treated as UNKNOWN → it does NOT advance the durable status watermark.
    assert next_state.last_market_status_epoch == 1
    assert next_state.last_market_status_recv_ts == 1


def test_status_epoch_rollback_treated_as_unknown() -> None:
    # The core re-checks epoch AND recv regression against its OWN durable state watermark, so a
    # rolled-back status generation is never accepted as fresher truth — durable across an assembler
    # restart that replays an older generation (REQ-026/AC-048).
    config = _config()
    state = _warm_state(last_market_status_epoch=5, last_market_status_recv_ts=99_995)

    # (a) epoch rollback: incoming epoch 4 < watermark 5; recv in-window AND not recv-regressed.
    epoch_rollback = _obs(
        market_status="ACTIVE",
        market_status_epoch=4,
        market_status_recv_ts=99_999,
        as_of_ts=100_000,
    )
    d_epoch, s_epoch = decide(epoch_rollback, state, config)
    assert d_epoch.kind == "NO_QUOTE"
    assert d_epoch.reason_codes == ("market_status_unknown",)
    assert s_epoch.last_market_status_epoch == 5  # watermark NEVER regresses
    assert s_epoch.last_market_status_recv_ts == 99_995

    # (b) recv rollback: incoming recv 99_990 < watermark 99_995; epoch equal AND in-window.
    recv_rollback = _obs(
        market_status="ACTIVE",
        market_status_epoch=5,
        market_status_recv_ts=99_990,
        as_of_ts=100_000,
    )
    d_recv, s_recv = decide(recv_rollback, state, config)
    assert d_recv.kind == "NO_QUOTE"
    assert d_recv.reason_codes == ("market_status_unknown",)
    assert s_recv.last_market_status_epoch == 5  # watermark unchanged
    assert s_recv.last_market_status_recv_ts == 99_995


def test_byte_identical_obs_differ_only_by_status_differ_in_eligibility() -> None:
    # Two observations byte-identical EXCEPT market_status: status is load-bearing on the placement
    # eligibility decision and NOTHING else — the venue-accumulator training + advanced state are
    # byte-identical, only the quote disposition flips (AC-041/RED-33).
    config = _config()
    state = _warm_state(last_market_status_epoch=1, last_market_status_recv_ts=1)
    active = _obs(market_status="ACTIVE", market_status_epoch=5, as_of_ts=100_000)
    halted = _obs(market_status="HALTED", market_status_epoch=5, as_of_ts=100_000)

    # The two observations differ in the market_status field ALONE.
    assert active.model_copy(update={"market_status": "HALTED"}) == halted

    d_active, s_active = decide(active, state, config)
    d_halted, s_halted = decide(halted, state, config)

    # Placement eligibility flips on status alone: ACTIVE places, HALTED does not.
    assert d_active.kind == "QUOTE_TWO_SIDED"
    assert d_halted.kind == "NO_QUOTE"
    assert d_halted.reason_codes == ("market_halted",)
    # Everything else — the trained/advanced state — is byte-identical: status touches ONLY the
    # quote disposition, never the venue accumulators or the watermark (both statuses are fresh gen 5).
    assert s_active == s_halted
    assert s_active.state_hash() == s_halted.state_hash()


def test_halted_closed_cancel_never_place() -> None:
    # HALTED / CLOSED never yield a placing decision: the gate emits NO_QUOTE with the status reason,
    # and — with exposure resting — E5-T3's cancel funnel compiles that into the single-phase cancel
    # plan (``cancel_all_orders`` then ``abstain``, ``cancel_exposure_first``) that WITHDRAWS the
    # exposure. (E3-T6 pinned that no fresh write is ever eligible under a halted/closed market,
    # exposure or not; E5-T3 now supplies the deferred cancel-plan INTENT — still never a place.)
    config = _config()
    state = _warm_state(last_market_status_epoch=1, last_market_status_recv_ts=1)
    for status, reason in (("HALTED", "market_halted"), ("CLOSED", "market_closed")):
        # Exposure present (a resting order + open position) — the cancel-on-exposure scenario.
        obs = _obs(
            market_status=status,
            market_status_epoch=5,
            as_of_ts=100_000,
            net_position=25.0,
            resting=(_resting_order(),),
        )
        decision, _ = decide(obs, state, config)
        assert decision.kind == "NO_QUOTE", status
        # The truthful status cause is preserved; the cancel funnel appends ``cancel_exposure_first``.
        assert decision.reason_codes == (reason, "cancel_exposure_first"), status
        # The exposure is actively withdrawn: cancel-ALL then abstain — never a place under
        # HALTED/CLOSED, and the plan is single-phase (no ``place_quote`` leg mixed in).
        assert [leg.kind for leg in decision.intent_plan] == [
            "cancel_all_orders",
            "abstain",
        ], status
        assert all(leg.kind != "place_quote" for leg in decision.intent_plan), status
        assert decision.kind != "QUOTE_TWO_SIDED"  # never a place under HALTED/CLOSED
