"""M6 (S5) Task 18/18b — ValueVsVenue: a real Agent that prices fair vs an INJECTED venue quote.

The agent compares the TxLINE de-margined fair probability against a venue decimal price supplied
ONLY through an injected ``venue_price_source`` — never read from the (evidence-sealed) market
state. Its ``vvv_signal`` core is pure: no quote ⇒ no edge. And — THE trust test — the emitted
``AgentAction.params`` carry ONLY TxLINE-derived fields (market_key/side/reason/confidence), never
a venue-derived value, because ``AgentAction.model_dump()`` is sealed into the ``evidence_hash``
(SEC-003 / INVARIANT 4: venue data enters via the injected source, never via the action).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_drift_agent import _ms
from tests.test_replay_pack import _write_session
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.window import RunWindow
from veridex.venues.polymarket import decimal_to_native, native_to_decimal
from veridex.venues.price_history import VenuePriceHistoryFrame
from veridex.venues.venue_price_source import (
    TimedVenueQuote,
    VenuePriceSource,
    build_backfilled_venue_source,
)

_FIXTURE_ID = 5

# ── Units regression fixtures (Run-002-VvV) ─────────────────────────────────────────────────
# TxLINE ``MarketState.ts`` is unix MILLISECONDS (13-digit); a ``VenuePriceSource`` is contractually
# keyed by unix SECONDS (Polymarket-canonical, matching the backfilled frames). These realistic
# mismatched-scale fixtures (a ms query ts vs seconds frames) drive a REAL ts-sensitive
# ``build_backfilled_venue_source`` — unlike the fixed ``_src`` stubs above, which ignore ``ts`` and
# so never exercised the unit contract. Every call site must convert ms→s or staleness ≈ 1e12 >> the
# 900s bound → source returns ``None`` → 0% coverage (the Run-002 artifact).
_FRAME_TS_EARLY_S = 1_782_641_900  # unix SECONDS (10-digit), Polymarket-canonical frame ts
_FRAME_TS_LATE_S = 1_782_642_000  # latest frame at/before the query
_QUERY_TS_MS = 1_782_642_003_000  # TxLINE decision ts in unix MILLISECONDS (13-digit) == 1_782_642_003 s
_EXPECTED_STALENESS_S = 3  # 1_782_642_003 - 1_782_642_000, comfortably inside the 900s bound


def _seconds_frames() -> list[VenuePriceHistoryFrame]:
    """Two backfilled 1X2|home frames at SECONDS-scale ts (matching real Polymarket frames)."""
    return [
        VenuePriceHistoryFrame(
            ts=ts,
            fixture_id=_FIXTURE_ID,
            market_ref="1X2|home|full",
            condition_id="0xcond",
            token_id="tok-home",
            native_price=decimal_to_native(2.0),
            venue_decimal_price=2.0,
            price_kind="clob-prices-history",
            fidelity_s=60,
        )
        for ts in (_FRAME_TS_EARLY_S, _FRAME_TS_LATE_S)
    ]


def _seconds_frame_source() -> VenuePriceSource:
    """A REAL ts-sensitive source over seconds-scale frames (900s freshness bound)."""
    src, _sid = build_backfilled_venue_source(
        _seconds_frames(),
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )
    return src


def _ms_query_state() -> MarketState:
    """A TxLINE tick whose ``ts`` is unix MILLISECONDS (13-digit) — the real decision-query scale."""
    return MarketState(
        fixture_id=_FIXTURE_ID,
        tick_seq=0,
        ts=_QUERY_TS_MS,
        phase=0,
        markets={
            "1X2|home|full": {
                "stable_prob_bps": {"home": 6000},
                "stable_price": {"home": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _q(price: float, *, staleness_s: int = 0) -> TimedVenueQuote:
    """A time-aligned venue quote at ``price`` (decimal odds) with a given staleness."""
    return TimedVenueQuote(venue_decimal_price=price, staleness_s=staleness_s)


def _src(price: float | None, *, staleness_s: int = 0) -> VenuePriceSource:
    """A 4-arg time-aligned source returning a FIXED quote (or ``None``) for every decision coordinate.

    Migrates the old market-key-only ``lambda mk: price`` stubs to the C-2 seam
    ``(fixture_id, market_key, side, ts) -> TimedVenueQuote | None`` without changing what they price.
    """
    quote = None if price is None else _q(price, staleness_s=staleness_s)
    return lambda fixture_id, market_key, side, ts: quote


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_vvv_report",
        fixture_id=_FIXTURE_ID,
        market_allowlist=["1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


def test_no_executable_edge_without_a_quote() -> None:
    """No venue quote ⇒ both edge fields are None and the signal does not fire (fail-safe)."""
    from veridex.strategies.value_vs_venue import vvv_signal

    signal = vvv_signal(6000, None)

    assert signal["gap_bps"] is None
    assert signal["estimated_executable_edge_bps"] is None
    assert signal["fired"] is False


def test_vvv_signal_rejects_a_native_q_masquerading_as_decimal() -> None:
    """F1: a native q (<= 1.0) passed where DECIMAL odds are required must fail-fast (AC-014 lesson).

    Decimal odds are ``1/q`` and thus ALWAYS > 1.0; a value <= 1.0 is a native-q misuse that would
    silently misprice (all-negative edge, never fires). Reject it at the boundary rather than lie.
    """
    from veridex.strategies.value_vs_venue import vvv_signal

    with pytest.raises(ValueError):
        vvv_signal(fair_prob_bps=6000, venue_decimal_price=0.50)  # 0.50 is native q, not decimal odds

    # None (no quote) and real decimal odds still work:
    assert vvv_signal(fair_prob_bps=6000, venue_decimal_price=None)["fired"] is False
    assert vvv_signal(fair_prob_bps=6000, venue_decimal_price=2.0)["estimated_executable_edge_bps"] is not None


def test_edge_uses_decimal_price_not_native_q() -> None:
    """Edge math consumes the DECIMAL price (via native_to_decimal), never the raw native q."""
    from veridex.strategies.value_vs_venue import vvv_signal

    # native q=0.50 -> decimal 2.0; fair 6000 bps (0.60): edge = 0.60*2.0 - 1 = +0.20 -> +2000 bps.
    signal = vvv_signal(6000, native_to_decimal(0.50))

    assert signal["gap_bps"] is not None
    assert signal["estimated_executable_edge_bps"] is not None
    assert signal["estimated_executable_edge_bps"] > 0
    assert signal["fired"] is True


def test_agent_is_a_real_veridex_agent_with_injected_venue_source() -> None:
    """The factory returns a REAL reproducible-proof Agent with a callable decide + config_hash."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="test-venue-src")
    snapshot = _ms(6000)

    assert agent.proof_mode == "reproducible"
    assert callable(agent.decide)
    assert agent.config_hash is not None
    # CALLABLE config_hash (the orchestrator's finalize calls config_hash(market_state)):
    assert agent.config_hash(snapshot) == agent.config_hash(snapshot)  # stable across calls
    other = value_vs_venue_agent(
        venue_price_source=_src(2.0), venue_source_id="test-venue-src", min_edge_bps=9999
    )
    assert agent.config_hash(snapshot) != other.config_hash(snapshot)  # param-sensitive


def test_venue_source_identity_is_bound_into_config_hash() -> None:
    """Reproducibility (Codex M6): the venue source identity is pinned in the agent's config_hash.

    ``decide`` reads ``venue_price_source`` (which flips fire/wait), so two agents differing ONLY in
    their venue source must NOT share a config_hash — otherwise "same config ⇒ same sealed decision"
    is false.
    """
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    ms = _ms(6000)
    a = value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="src-A", min_edge_bps=0)
    b = value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="src-B", min_edge_bps=0)
    assert a.config_hash(ms) != b.config_hash(ms)  # different venue source -> different config hash

    c = value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="src-A", min_edge_bps=0)
    assert a.config_hash(ms) == c.config_hash(ms)  # same source -> same (stable) hash


def test_vvv_agent_rejects_missing_venue_source_identity() -> None:
    """A reproducible VvV agent REFUSES to exist without a bound venue source identity."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    with pytest.raises((TypeError, ValueError)):
        value_vs_venue_agent(venue_price_source=_src(2.0), min_edge_bps=0)  # no venue_source_id
    with pytest.raises(ValueError):
        value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="", min_edge_bps=0)


def test_vvv_action_params_do_not_smuggle_venue_data_into_evidence() -> None:
    """THE trust test: the fired action's params carry NO venue-derived value (INVARIANT 4)."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(venue_price_source=_src(2.0), venue_source_id="test-venue-src")
    action = asyncio.run(agent.decide(_ms(6000)))

    assert action.type != "WAIT", "fair 6000 @ decimal 2.0 clears a 0 min-edge and must fire"
    # params are a SUBSET of the TxLINE-derived allowlist — nothing else may ride into the seal.
    assert set(action.params) <= {"market_key", "side", "reason", "confidence"}
    # ...and specifically NONE of the venue-derived quantities leak in as params.
    for forbidden in ("venue_decimal_price", "estimated_executable_edge_bps", "gap_bps"):
        assert forbidden not in action.params
    # Defence in depth: the whole sealed action_payload must contain no venue-derived number.
    for forbidden_value in ("2000", "1000", "2.0"):
        assert forbidden_value not in action.model_dump_json()


# ------------------------------------------------------------------------------------------
# C-2 — the time-aligned seam: the agent consumes a 4-arg TimedVenueQuote source (CON-002/006).
# ------------------------------------------------------------------------------------------


def test_venue_numbers_never_in_action_params() -> None:
    """CON-002: NONE of a quote's venue numbers (price/gap/edge/staleness) ride into the sealed params."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    # fair 0.55 (5500 bps) @ decimal 1.90 → edge = 0.55*1.90-1 = +0.045 → clears the 0 min-edge, fires.
    agent = value_vs_venue_agent(
        agent_id="vvv", venue_price_source=_src(1.90, staleness_s=120), venue_source_id="src#1", min_edge_bps=0
    )
    action = asyncio.run(agent.decide(_ms(5500)))

    assert action.type != "WAIT", "fair 0.55 @ decimal 1.90 clears a 0 min-edge and must fire"
    for forbidden in ("venue_decimal_price", "gap_bps", "estimated_executable_edge_bps", "staleness", "staleness_s"):
        assert forbidden not in action.params  # CON-002 — no venue number in the sealed action
    # Defence in depth: neither the price (1.9) nor the staleness (120) appears anywhere in the payload.
    payload = action.model_dump_json()
    for forbidden_value in ("1.9", "120", "450"):
        assert forbidden_value not in payload


def test_agent_prices_ms_decision_ts_against_seconds_frames_and_fires() -> None:
    """Units regression (Run-002-VvV): a TxLINE ms decision ts priced against seconds frames MUST match.

    ``MarketState.ts`` is unix MILLISECONDS but the source is keyed by unix SECONDS. The ``decide`` call
    site must convert ms→s before querying, so the (900s-bounded) source returns the latest frame at 3s
    staleness and the agent FIRES (fair 6000 bps @ decimal 2.0 → edge +0.20). On the pre-fix code the raw
    ms ts made staleness ≈ 1.78e12 ≫ 900 → source ``None`` → the agent WAITs on every tick (the Run-002
    0%-coverage artifact). The fixed ``_src`` stubs above ignore ``ts`` and never caught this — only a
    REAL ts-sensitive ``build_backfilled_venue_source`` does.
    """
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(venue_price_source=_seconds_frame_source(), venue_source_id="src-seconds")
    action = asyncio.run(agent.decide(_ms_query_state()))

    assert action.type != "WAIT", (
        "a ms decision ts vs seconds frames must convert to seconds and MATCH (staleness 3s ≤ 900), "
        "not overrun the freshness bound → None → WAIT"
    )
    assert action.params.get("market_key") == "1X2|home|full"
    assert action.params.get("side") == "home"


def test_agent_waits_when_source_returns_none() -> None:
    """A ``None`` from the source (missing/too-stale at the C-4 bound) ⇒ no edge ⇒ the agent WAITs."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(agent_id="vvv", venue_price_source=_src(None), venue_source_id="src#1")
    action = asyncio.run(agent.decide(_ms(5500)))

    assert action.type == "WAIT"  # AgentAction.type, not .action


def test_same_quote_different_venue_source_id_changes_raw_prescore_config_not_evidence() -> None:
    """Codex M3 (both halves): the venue source IDENTITY moves config identity, NOT the sealed evidence.

    Two agents given the SAME quote but a DIFFERENT ``venue_source_id`` take the SAME action (venue
    numbers absent from the params), so their sealed ``run_events`` / ``evidence_hash`` are byte-identical
    — while their raw-prescore ``model_prompt_config_hash`` (config identity) differs. ``config_hash``
    lives on the OTHER side of the seal from the action; it NEVER enters ``compute_evidence_hash``.
    """
    from veridex.runtime.orchestrator import run_competition
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    src = _src(1.90, staleness_s=60)  # ONE quote shape, priced identically for both agents
    a = value_vs_venue_agent(agent_id="vvv", venue_price_source=src, venue_source_id="src#A")
    b = value_vs_venue_agent(agent_id="vvv", venue_price_source=src, venue_source_id="src#B")

    res_a = asyncio.run(run_competition([_ms(5500)], [a], source_mode="replay"))
    res_b = asyncio.run(run_competition([_ms(5500)], [b], source_mode="replay"))

    def _fired_prescore(res: object) -> dict:
        fired = [
            row["raw_prescore"]
            for row in res.score_rows  # type: ignore[attr-defined]
            if row["raw_prescore"]["raw_action"]["type"] == "FOLLOW_MOMENTUM"
        ]
        assert fired, "fair 0.55 @ decimal 1.90 clears a 0 min-edge and must fire"
        return fired[0]

    ps_a, ps_b = _fired_prescore(res_a), _fired_prescore(res_b)

    # (i) SAME quote → identical sealed action payload → identical evidence_hash (config-independent).
    assert ps_a["raw_action"] == ps_b["raw_action"]  # venue numbers absent → byte-identical payload
    assert res_a.evidence_hash == res_b.evidence_hash
    # (ii) DIFFERENT venue_source_id → different raw-prescore config identity...
    assert ps_a["model_prompt_config_hash"] != ps_b["model_prompt_config_hash"]
    # ...yet the sealed evidence carries NO venue number (the 1.90 price never leaks into run_events).
    events_repr = str(res_a.run_events)
    for forbidden in ("1.9", "venue_decimal_price", "staleness", "gap_bps", "estimated_executable_edge_bps"):
        assert forbidden not in events_repr


def test_value_vs_venue_strategy_is_accepted_by_preflight() -> None:
    """Step 6: value-vs-venue is a first-class deploy strategy the preflight ``config`` check accepts."""
    from veridex.deploy.preflight import DeployConfig, run_deploy_preflight

    config = DeployConfig(
        template_id="value-vs-venue",
        agent_id="studio-vvv",
        strategy="value-vs-venue",
        source_mode="replay",
    )
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = next(c for c in checks if c.name == "config")
    assert cfg.ok is True


# ------------------------------------------------------------------------------------------
# Task 18b — the VvV PRODUCER: a real BacktestReport with a POST-BUILD estimated edge (S5).
# ------------------------------------------------------------------------------------------


async def test_vvv_produces_report_with_estimated_edge_but_scored_path_stays_venue_blind(tmp_path: Path) -> None:
    """The producer attaches an estimated edge POST-build; the scored/ranked path stays venue-blind."""
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge

    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)

    assumptions = {"no_interpolation": True, "slippage_bps": 0, "costs_bps": 0}
    # venue decimal 5.0 makes every fixture-5 side's edge strongly positive, so the agent FIRES —
    # the estimated edge then reflects an ACTUAL taken pick (F2), not an unfired opportunity.
    result, report = await vvv_report_with_estimated_edge(
        pack_dir,
        _FIXTURE_ID,
        venue_price_source=_src(5.0),
        venue_source_id="test-venue-src",
        window=_window(),
        min_edge_bps=0,
        assumptions=assumptions,
    )

    # An estimated (venue-derived) edge IS produced, with a machine-readable rung + explicit assumptions.
    assert report.estimated_executable_edge_bps is not None
    assert report.estimated_edge_rung in {"backfilled-price-history", "recorded-live-quote"}
    assert report.estimated_edge_assumptions["no_interpolation"] is True
    # ...but the REAL executable edge stays null (paper venue — no live fill).
    assert report.real_executable_edge_bps is None
    # ...and the estimated edge NEVER enters any ranked leaderboard row (SEC-005).
    for row in report.leaderboard:
        assert "estimated_executable_edge_bps" not in row
    # The report is for the same sealed run the producer scored.
    assert report.run_id == result.run_id


def _write_1x2_session(tmp_path: Path, pct: list[float]) -> Path:
    """A real, hashed 1X2 pack (fixture 5) whose de-vigged fair probs come from ``pct`` (percent).

    Two identical ticks so the pre_match window splits the last as the reconstructed close and the
    agent decides on exactly one tick.
    """
    from veridex.ingest.recorder import SessionMeta, envelope_line

    def _rec(ts: int) -> dict:
        return {
            "FixtureId": _FIXTURE_ID,
            "Ts": ts,
            "InRunning": False,
            "SuperOddsType": "1X2",
            "MarketPeriod": None,
            "MarketParameters": None,
            "PriceNames": ["Home", "Draw", "Away"],
            "Prices": [2500, 3200, 2800],
            "Pct": pct,
        }

    session_dir = tmp_path / "s"
    session_dir.mkdir()
    (session_dir / "records.jsonl").write_text(
        envelope_line(_rec(100_000), 100) + "\n" + envelope_line(_rec(131_000), 131) + "\n"
    )
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)
    return pack_dir


def _fired_picks(result) -> list[dict]:
    """The (market_key, side) params of the agent's ACTUAL fired (FOLLOW_MOMENTUM) sealed picks."""
    return [
        row["raw_prescore"]["raw_action"]["params"]
        for row in result.score_rows
        if row.get("raw_prescore", {}).get("raw_action", {}).get("type") == "FOLLOW_MOMENTUM"
    ]


async def test_estimated_edge_is_none_when_the_strategy_fires_no_picks(tmp_path: Path) -> None:
    """F2: a strategy that takes ZERO positions reports NO estimated edge — never an unfired one.

    On fixture-5 every side is < 5000 bps, so at decimal 2.0 every edge is NEGATIVE and the agent
    WAITs on every tick. The old global-max aggregate would still report the least-negative UNFIRED
    opportunity; the honest answer is ``None`` (the strategy took nothing to estimate an edge over).
    """
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge

    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)

    result, report = await vvv_report_with_estimated_edge(
        pack_dir,
        _FIXTURE_ID,
        venue_price_source=_src(2.0),
        venue_source_id="test-venue-src",
        window=_window(),
        min_edge_bps=0,
        assumptions={"no_interpolation": True},
    )

    assert _fired_picks(result) == []  # the strategy took ZERO positions...
    assert report.estimated_executable_edge_bps is None  # ...so there is no estimated edge to report


async def test_estimated_edge_reflects_the_fired_pick_not_a_larger_unfired_opportunity(tmp_path: Path) -> None:
    """F2: the reported edge is the STRATEGY'S taken pick's edge, not the best market opportunity.

    Pct makes Home (4000 bps) the max-fair side, but ``Away`` (3500 bps) sorts first, so the agent
    FIRES on Away and never takes Home. The old global-max aggregate reported Home's (larger) edge —
    an opportunity the strategy declined; the honest answer is Away's edge (the position it took).
    """
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge
    from veridex.strategies.value_vs_venue import vvv_signal

    pack_dir = _write_1x2_session(tmp_path, [40.0, 25.0, 35.0])  # Home 4000 > Away 3500 > Draw 2500

    result, report = await vvv_report_with_estimated_edge(
        pack_dir,
        _FIXTURE_ID,
        venue_price_source=_src(3.0),
        venue_source_id="test-venue-src",
        window=_window(),
        min_edge_bps=-(10**9),  # everything clears → agent fires the first-sorted side (Away)
        assumptions={"no_interpolation": True},
    )

    picks = _fired_picks(result)
    assert picks and picks[0]["side"] == "Away"  # the strategy actually took Away, not Home

    away_edge = vvv_signal(3500, 3.0)["estimated_executable_edge_bps"]  # the FIRED pick's edge
    home_edge = vvv_signal(4000, 3.0)["estimated_executable_edge_bps"]  # the larger UNFIRED edge
    assert away_edge < home_edge  # the discrimination is real (Away < Home)
    assert report.estimated_executable_edge_bps == away_edge  # reflects the FIRED pick...
    assert report.estimated_executable_edge_bps != home_edge  # ...NOT the larger unfired opportunity
