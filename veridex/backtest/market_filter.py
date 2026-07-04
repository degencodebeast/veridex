"""FU-2 — the eligible-market allowlist + manifest that gate the S6 producer's scored universe.

The M1 filter (:func:`veridex.strategies.market_quality.evaluate_market_quality`) is an ELIGIBILITY
gate, NOT a scoring change: it decides WHICH markets are worth scoring (degenerate near-certain lines,
thin ticks, short horizon, unmapped, or a close that never priced), and it NEVER touches how CLV is
computed. This module turns that per-market verdict into two things the producer needs:

  * :func:`build_eligible_market_manifest` — derives each market's filter inputs from a fixture's
    replayed ticks, runs the M1 filter under a pinned :class:`~veridex.strategies.market_quality.MarketQualityConfig`,
    and emits a deterministic, machine-readable :class:`EligibleMarketManifest`
    (``filter_config_hash`` + the exact eligible market_keys + the excluded market_keys with their
    named exclusion reasons + counts + a ``zero_eligible`` named-skip flag). Codex's hard rule: no
    filter claim without this manifest, so a Run-001 protocol can pin the exact market universe.
  * :func:`filter_marketstates_to_allowlist` — restricts a tape of :class:`~veridex.ingest.marketstate.MarketState`
    to an eligible full-key allowlist, so the SAME eligible universe is fed to drift AND baselines
    (apples-to-apples), once, upstream, before any CLV is computed.

Filter inputs are derived from the PRE-KICKOFF (``phase == 0``) ticks — the decision universe a
``pre_match`` backtest scores (D2) — so the manifest describes exactly the markets that will be scored.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from pydantic import BaseModel

from veridex.ingest.marketstate import MarketState
from veridex.strategies.market_quality import MarketQualityConfig, evaluate_market_quality


class ExcludedMarket(BaseModel):
    """One excluded market and every named rule that failed (never a single generic 'ineligible')."""

    market_key: str
    reasons: list[str]


class EligibleMarketManifest(BaseModel):
    """The reproducible eligible-market artifact for one fixture (the required FU-2 output).

    Attributes:
        fixture_id: The fixture this manifest scopes.
        filter_config_hash: The pinned config identity (fixed BEFORE results) — config-only, stable.
        eligible: The exact eligible market_keys that WILL be scored (sorted, deterministic).
        excluded: The excluded market_keys, each with its named exclusion reasons (sorted by key).
        eligible_count: ``len(eligible)``.
        excluded_count: ``len(excluded)``.
        zero_eligible: ``True`` when NO market survived the filter — a NAMED skip (never a silent empty).
    """

    fixture_id: int
    filter_config_hash: str
    eligible: list[str]
    excluded: list[ExcludedMarket]
    eligible_count: int
    excluded_count: int
    zero_eligible: bool


def _pre_kickoff_states(marketstates: Iterable[MarketState]) -> list[MarketState]:
    """The pre-kickoff (``phase == 0``) ticks — the decision universe a ``pre_match`` backtest scores."""
    return [state for state in marketstates if state.phase == 0]


def build_eligible_market_manifest(
    fixture_id: int, marketstates: list[MarketState], config: MarketQualityConfig
) -> EligibleMarketManifest:
    """Run the M1 filter over a fixture's markets and emit the deterministic eligible-market manifest.

    For each market seen in a pre-kickoff tick the filter inputs are derived from the observable
    pre-kickoff series: ``tick_count`` is how many pre-kickoff ticks priced it, ``horizon_s`` is the
    span from its first to its last pre-kickoff tick, and ``implied_prob`` / ``mapping_valid`` /
    ``close_quality`` come from its LAST pre-kickoff line (the value nearest the decision boundary).
    ``implied_prob`` is the MAX side probability, so a near-certain line is caught whichever side is
    lopsided (e.g. an O/U 0.5 "over" at 0.97). Every failing rule is surfaced by name (never hidden).

    Args:
        fixture_id: The fixture being filtered.
        marketstates: The fixture's ordered replayed ticks.
        config: The pinned quality thresholds (its ``filter_config_hash`` is stamped into the manifest).

    Returns:
        The :class:`EligibleMarketManifest` — eligible/excluded market_keys + reasons + counts + hash.
    """
    phase0 = _pre_kickoff_states(marketstates)

    tick_count: dict[str, int] = {}
    first_ts: dict[str, int] = {}
    last_ts: dict[str, int] = {}
    last_market: dict[str, dict] = {}
    for state in phase0:  # ordered: later ticks overwrite last_ts/last_market
        for market_key, market in state.markets.items():
            tick_count[market_key] = tick_count.get(market_key, 0) + 1
            first_ts.setdefault(market_key, state.ts)
            last_ts[market_key] = state.ts
            last_market[market_key] = market

    eligible: list[str] = []
    excluded: list[ExcludedMarket] = []
    for market_key in sorted(tick_count):  # deterministic ordering
        market = last_market[market_key]
        prob_bps = market.get("stable_prob_bps") or {}
        mapping_valid = bool(prob_bps)
        # implied_prob is the MAX side probability (catches a near-certain line whichever side is
        # lopsided). When the market is UNMAPPED (no prob at the close), near-certainty is undefined —
        # pass a mid-band neutral so we don't MISLABEL an unmapped/suspended market "near_certain"; it
        # is still excluded, honestly, by the ``unmapped`` / ``close_*`` reasons instead.
        implied_prob = max(prob_bps.values()) / 10_000.0 if prob_bps else 0.5
        suspended = bool(market.get("suspended", False))
        close_quality = "suspended" if suspended else ("priced" if prob_bps else "missing")

        result = evaluate_market_quality(
            market_ref=market_key,
            implied_prob=implied_prob,
            tick_count=tick_count[market_key],
            horizon_s=last_ts[market_key] - first_ts[market_key],
            mapping_valid=mapping_valid,
            close_quality=close_quality,
            config=config,
        )
        if result.eligible:
            eligible.append(market_key)
        else:
            excluded.append(ExcludedMarket(market_key=market_key, reasons=result.reasons))

    return EligibleMarketManifest(
        fixture_id=fixture_id,
        filter_config_hash=config.filter_config_hash(),
        eligible=eligible,
        excluded=excluded,
        eligible_count=len(eligible),
        excluded_count=len(excluded),
        zero_eligible=not eligible,
    )


def filter_marketstates_to_allowlist(
    marketstates: list[MarketState], allowlist: Collection[str]
) -> list[MarketState]:
    """Restrict each tick's markets to the eligible full-key ``allowlist`` (EXACT membership, not prefix).

    Every tick is preserved (so the D2 pre-kickoff/kickoff phase structure is untouched) — only its
    ``markets`` map is narrowed to the eligible keys. Feeding the SAME filtered tape to drift and to the
    baselines keeps the scored universe identical for both, and the law computes CLV over exactly the
    eligible markets — the CLV computation itself is never altered.
    """
    allowed = set(allowlist)
    return [
        state.model_copy(
            update={"markets": {k: v for k, v in state.markets.items() if k in allowed}}
        )
        for state in marketstates
    ]
