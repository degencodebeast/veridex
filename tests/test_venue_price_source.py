"""C-2 (S5) — the time-aligned venue seam: ``TimedVenueQuote`` + the ``VenuePriceSource`` type.

The seam is the ONLY door venue data enters the VvV trust path. A source is keyed by the DECISION
coordinate ``(fixture_id, market_key, side, ts)`` — so a quote is time-aligned to the tick the agent
decides on — and returns a :class:`TimedVenueQuote` (a decimal price + its measured ``staleness_s``)
or ``None`` when no quote is available at/under the caller's freshness bound (CON-006: no
interpolation; missing/too-stale ⇒ ``None``). The numbers a quote carries are venue numbers and
NEVER enter ``AgentAction.params`` / evidence (CON-002) — that boundary is proven in the agent tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veridex.venues.polymarket import decimal_to_native
from veridex.venues.price_history import VenuePriceHistoryFrame
from veridex.venues.venue_price_source import (
    TimedVenueQuote,
    VenuePriceSource,
    build_backfilled_venue_source,
)


def test_timed_venue_quote_carries_decimal_price_and_staleness() -> None:
    """A quote is a decimal price time-aligned to a decision, plus the staleness of that alignment."""
    q = TimedVenueQuote(venue_decimal_price=1.90, staleness_s=120)

    assert q.venue_decimal_price == 1.90
    assert q.staleness_s == 120


def test_timed_venue_quote_requires_both_fields() -> None:
    """Both fields are load-bearing: a quote with no staleness can't be freshness-bounded (CON-006)."""
    with pytest.raises(ValidationError):
        TimedVenueQuote(venue_decimal_price=1.90)  # type: ignore[call-arg]  # missing staleness_s
    with pytest.raises(ValidationError):
        TimedVenueQuote(staleness_s=120)  # type: ignore[call-arg]  # missing venue_decimal_price


def test_venue_price_source_is_a_4arg_time_aligned_quote_callable() -> None:
    """A conforming source is keyed by ``(fixture_id, market_key, side, ts)`` → ``TimedVenueQuote | None``."""
    src: VenuePriceSource = lambda fixture_id, market_key, side, ts: (
        TimedVenueQuote(venue_decimal_price=2.0, staleness_s=0) if side == "home" else None
    )

    quote = src(5, "1X2|home", "home", 1000)
    assert quote is not None
    assert quote.venue_decimal_price == 2.0
    # No quote for the coordinate the source can't price → None (fail-safe; the agent WAITs).
    assert src(5, "1X2|home", "away", 1000) is None


# ── C-4: bounded-staleness time-indexed source + venue_source_id ────────────────────────────────
#
# The source over BACKFILLED frames adds the time-alignment C-2's seam only fixed the SHAPE of:
# on a decision ``(fixture_id, market_key, side, ts)`` it returns the LATEST frame at/before ``ts``
# whose age ≤ ``freshness_s`` (never interpolated, never a look-ahead to a later frame), else
# ``None`` (CON-006). ``venue_source_id`` is a reproducibility hash over the five identity inputs
# (the C-3 Pack ``artifact_content_hash``es + the C-1 coverage hash + freshness + haircut ladder +
# config version); ANY change flips it, so the agent's ``config_hash`` pins exactly what it priced
# against. The haircut ladder is carried for identity/reporting ONLY — it never touches the returned
# RAW ``venue_decimal_price`` (CON-007; REQ-005).
#
# The WC 1X2 ``market_ref`` ("1X2|home|full", per polymarket_resolver) already embeds the side, and
# the VvV agent passes that SAME string as ``market_key`` — so the lookup keys on ``market_key`` and
# the ``side`` arg is a redundant confirmation of what the ref already names.


def _frames_for(
    side: str, quotes: list[tuple[int, float]], *, fixture_id: int = 1
) -> list[VenuePriceHistoryFrame]:
    """Build synthetic frames for one WC 1X2 side with EXACT decimal prices at explicit timestamps.

    ``venue_decimal_price`` is stored exactly as given and ``native_price`` is its structural inverse
    (``decimal_to_native``), so the AC-014 fail-closed validator passes AND a returned quote's price
    is byte-for-byte the literal the test asserts against.
    """
    market_ref = f"1X2|{side}|full"
    return [
        VenuePriceHistoryFrame(
            ts=ts,
            fixture_id=fixture_id,
            market_ref=market_ref,
            condition_id="0xcond",
            token_id=f"tok-{side}",
            native_price=decimal_to_native(decimal),
            venue_decimal_price=decimal,
            price_kind="clob-prices-history",
            fidelity_s=60,
        )
        for ts, decimal in quotes
    ]


_frames = _frames_for("home", quotes=[(600, 1.61), (1200, 1.72)])


def test_bounded_staleness_no_lookahead_no_interp() -> None:
    """Latest quote at/before ts within freshness → quote; too-stale/no-quote-before → None (no interp)."""
    frames = _frames_for("home", quotes=[(600, 1.61), (1200, 1.72)])  # (ts, decimal)
    src, sid = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )

    q = src(1, "1X2|home|full", "home", 1500)  # last<=1500 is 1200, age 300 <= 900
    assert q is not None
    assert q.venue_decimal_price == 1.72 and q.staleness_s == 300
    assert src(1, "1X2|home|full", "home", 2200) is None  # age 1000 > 900 -> None (no interp)
    assert src(1, "1X2|home|full", "home", 500) is None  # nothing at/before -> None (no lookahead)
    assert sid  # venue_source_id non-empty


def test_venue_source_id_binds_all_identity_inputs() -> None:
    """Changing ANY of the five identity inputs yields a different venue_source_id (reproducibility)."""
    base = {
        "price_history_artifact_hashes": ["ph#1"],
        "coverage_artifact_hash": "cov#1",
        "freshness_s": 900,
        "haircut_ladder_bps": [0, 100, 200, 300],
        "source_config_version": "cp1-v1",
    }
    _, sid0 = build_backfilled_venue_source(_frames, **base)  # type: ignore[arg-type]
    for changed in (
        {"price_history_artifact_hashes": ["ph#2"]},
        {"coverage_artifact_hash": "cov#2"},
        {"freshness_s": 600},
        {"haircut_ladder_bps": [0, 200]},
        {"source_config_version": "cp1-v2"},
    ):
        _, sid = build_backfilled_venue_source(_frames, **{**base, **changed})  # type: ignore[arg-type]
        assert sid != sid0  # any identity input change ⇒ new venue_source_id


def test_venue_source_id_is_deterministic_and_hash_order_independent() -> None:
    """Same inputs → same id; the artifact-hash LIST is order-independent (spec: sorted())."""
    base = {
        "coverage_artifact_hash": "cov#1",
        "freshness_s": 900,
        "haircut_ladder_bps": [0, 100, 200, 300],
        "source_config_version": "cp1-v1",
    }
    _, a = build_backfilled_venue_source(_frames, price_history_artifact_hashes=["ph#1", "ph#2"], **base)  # type: ignore[arg-type]
    _, b = build_backfilled_venue_source(_frames, price_history_artifact_hashes=["ph#1", "ph#2"], **base)  # type: ignore[arg-type]
    assert a == b  # deterministic for identical inputs
    _, c = build_backfilled_venue_source(_frames, price_history_artifact_hashes=["ph#2", "ph#1"], **base)  # type: ignore[arg-type]
    assert a == c  # order of the artifact-hash list must not matter (hashes are sorted)


def test_exact_tick_and_boundary_staleness() -> None:
    """A quote AT ts has staleness 0; a quote exactly ``freshness_s`` old is still IN (age ≤ bound)."""
    frames = _frames_for("home", quotes=[(600, 1.61), (1200, 1.72)])
    src, _ = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )

    exact = src(1, "1X2|home|full", "home", 1200)  # frame AT the tick
    assert exact is not None and exact.staleness_s == 0 and exact.venue_decimal_price == 1.72

    boundary = src(1, "1X2|home|full", "home", 2100)  # 1200 + 900 == freshness bound, still IN
    assert boundary is not None and boundary.staleness_s == 900 and boundary.venue_decimal_price == 1.72

    assert src(1, "1X2|home|full", "home", 2101) is None  # one second past the bound → None


def test_haircut_ladder_is_report_only_does_not_change_quote() -> None:
    """The source returns the RAW decimal price regardless of the haircut ladder (CON-007/REQ-005)."""
    frames = _frames_for("home", quotes=[(600, 1.61), (1200, 1.72)])
    src_a, _ = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )
    src_b, _ = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 500, 900],  # a WILDLY different ladder
    )

    qa = src_a(1, "1X2|home|full", "home", 1500)
    qb = src_b(1, "1X2|home|full", "home", 1500)
    assert qa is not None and qb is not None
    # Same RAW price + staleness: the ladder is identity/reporting only, never applied to the quote.
    assert qa.venue_decimal_price == qb.venue_decimal_price == 1.72
    assert qa.staleness_s == qb.staleness_s == 300


def test_missing_market_and_fixture_return_none() -> None:
    """A coordinate with no frames (unknown fixture or market) prices to None (fail-safe WAIT)."""
    src, _ = build_backfilled_venue_source(
        _frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )
    assert src(1, "1X2|away|full", "away", 1500) is None  # no frames for this market_ref
    assert src(999, "1X2|home|full", "home", 1500) is None  # no frames for this fixture
