"""B3 — the deterministic law: CLV recompute (REQ-104 / AC-104). Test-driven.

TRUST PATH: this module MUST NOT import agno/openai/anthropic/litellm (CON-007). It is the
load-bearing scoring law — the entire leaderboard ranks on the CLV definition computed here.

Contract (spec §4 — "Data contract — CLV / ClosingSnapshot"):

  recompute(entry_state, action, *, closing, source_mode="replay")
    -> {edge_bps, clv_bps|"pending", kelly_fraction, valid: bool, reason: str}

  clv_bps = closing.stable_prob_bps[side] - entry.stable_prob_bps[side]   # de-vigged prob, bps

Resolved §4 judgment calls (surfaced for the codex gate):

  * replay vs live — chosen via the explicit keyword `source_mode` ("replay" | "live"):
      - replay: `closing` MUST be the MarketState at horizon H for that market_key. A missing
        closing (None) or a closing that lacks the market_key is INVALID (replay has no future
        tick to wait for — the fixture is complete).
      - live:   `closing is None` means no later horizon tick exists yet, so clv_bps == "pending"
        (a VALID, not-yet-scored action). Once a later tick is supplied as `closing`, it scores
        exactly like replay.
  * edge_bps vs clv_bps — Phase 1 has NO independent fair-value source, so the only evidence-
    derived edge is the closing-line value itself. Therefore `edge_bps == clv_bps` (both ints,
    computed solely from recomputed `stable_prob_bps`). The LLM-claimed edge in `action.params`
    is read out only to be discarded (recorded as untrusted metadata via compute_clv_check).
  * WAIT — a valid abstention, never scored: clv_bps == "pending" (a non-numeric sentinel so the
    scorer (B6) excludes it from the CLV mean rather than counting it as 0), reason "wait_unscored".
  * kelly_fraction — advisory/risk-sizing only, computed at ENTRY from stable_prob_bps + stable_price,
    clamped to [0,1]. It is NEVER a score axis and NEVER affects `valid`.
  * Invalidity ordering — at entry and at closing the market is checked: absent -> suspended ->
    side-missing (Pct="NA" collapses to empty stable_prob_bps, i.e. side-missing). Entry is
    validated before closing so the most upstream failure wins the reason code.
"""
from __future__ import annotations

from typing import Any, Literal

from veridex.checks.clv import compute_clv_check
from veridex.ingest.marketstate import MarketState
from veridex.runtime.schemas import AgentAction, SportsActionType

REPLAY = "replay"
LIVE = "live"
PENDING = "pending"  # non-numeric clv_bps sentinel (live-awaiting-close and WAIT abstentions)


def _clamp01(x: float) -> float:
    """Clamp to the unit interval [0, 1]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _kelly_fraction(market: dict[str, Any], side: str) -> float:
    """Advisory Kelly at entry: f* = (b*p - q)/b, clamped to [0,1].

    p = stable_prob_bps[side]/10000 (de-vigged consensus prob); b = stable_price[side] - 1
    (net decimal odds). Returns 0.0 when the price is missing or non-positive (b<=0) — there is
    no sizable edge to advise. Never raises; advisory only, never gates validity.
    """
    prob_bps = market.get("stable_prob_bps", {})
    price = market.get("stable_price", {})
    if side not in prob_bps or side not in price:
        return 0.0
    try:
        decimal_price = float(price[side])
    except (TypeError, ValueError):
        # A non-numeric stable_price (e.g. "NA") must never raise — degrade to no advice.
        return 0.0
    b = decimal_price - 1.0
    if b <= 0.0:
        return 0.0
    p = prob_bps[side] / 10000.0
    q = 1.0 - p
    return _clamp01((b * p - q) / b)


def _invalid(reason: str, *, kelly: float = 0.0) -> dict[str, Any]:
    """An unscored/invalid verdict. kelly is advisory and carried through regardless."""
    return {"edge_bps": 0, "clv_bps": 0, "kelly_fraction": kelly, "valid": False, "reason": reason}


def _validate_market(state: MarketState, market_key: str, side: str, *, where: str) -> str | None:
    """Return a reason code if the market is unusable at `where` ("entry"/"closing"), else None.

    Order: absent -> suspended -> side-missing. Pct="NA" yields an empty stable_prob_bps, so an
    NA outcome is caught by the side-missing branch (and, per the normalizer, also suspended).
    """
    market = state.markets.get(market_key)
    if market is None:
        return f"{where}_market_absent"
    if market.get("suspended"):
        return f"{where}_suspended"
    if side not in market.get("stable_prob_bps", {}):
        return f"{where}_side_missing"
    return None


def recompute(
    entry_state: MarketState,
    action: AgentAction,
    *,
    closing: MarketState | None,
    source_mode: Literal["replay", "live"] = REPLAY,
) -> dict[str, Any]:
    """Deterministically recompute edge / CLV / Kelly / validity from evidence only (§4)."""
    if source_mode not in (REPLAY, LIVE):
        # Reject unknown modes loudly: a misspelling must never silently fall through to
        # replay semantics (which would wrongly mark a live, not-yet-closed action invalid).
        raise ValueError(f"source_mode must be 'replay' or 'live', got {source_mode!r}")

    # WAIT — valid abstention, never scored.
    if action.type == SportsActionType.WAIT:
        return {"edge_bps": 0, "clv_bps": PENDING, "kelly_fraction": 0.0, "valid": True, "reason": "wait_unscored"}

    params = action.params or {}
    market_key = params.get("market_key")
    side = params.get("side")
    if not market_key:
        return _invalid("market_key_missing")
    if not side:
        return _invalid("side_missing")

    # Entry must be usable first (most upstream failure wins).
    entry_reason = _validate_market(entry_state, market_key, side, where="entry")
    if entry_reason is not None:
        return _invalid(entry_reason)

    # Kelly is sized at entry; computed now so it rides along on every later verdict.
    kelly = _kelly_fraction(entry_state.markets[market_key], side)

    # No closing tick: replay => invalid; live => pending (awaiting a later horizon tick).
    if closing is None:
        if source_mode == LIVE:
            return {"edge_bps": 0, "clv_bps": PENDING, "kelly_fraction": kelly, "valid": True, "reason": "pending_closing"}
        return _invalid("closing_missing", kelly=kelly)

    closing_reason = _validate_market(closing, market_key, side, where="closing")
    if closing_reason is not None:
        return _invalid(closing_reason, kelly=kelly)

    entry_bps = entry_state.markets[market_key]["stable_prob_bps"][side]
    closing_bps = closing.markets[market_key]["stable_prob_bps"][side]
    clv_bps = closing_bps - entry_bps

    # Reuse the CLV Check: recompute on evidence only; the claimed edge is recorded-but-untrusted.
    claimed_edge_bps = params.get("claimed_edge_bps", params.get("edge_bps"))
    check = compute_clv_check(recomputed_edge_bps=clv_bps, claimed_edge_bps=claimed_edge_bps)

    return {
        "edge_bps": clv_bps,  # Phase 1: recomputed edge == closing-line value (no independent fair value).
        "clv_bps": clv_bps,
        "kelly_fraction": kelly,
        "valid": True,
        "reason": check.reason,
    }
