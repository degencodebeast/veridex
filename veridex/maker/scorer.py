"""MM-R1 forward-markout scorer with full quote accounting.

Scores every proposed :class:`~veridex.maker.contracts.TargetQuote` on
*quote quality only* — how the reference fair value moves relative to the
quoted price over a set of forward horizons. This is deliberately **not** a
fill/PnL/executable-edge measure: a :class:`QuoteMarkout` never carries a
``real_executable_edge_bps``/``pnl``/``fill_price`` field.

Accounting invariant (HB-10 / AC-015): every ``(quote, horizon)`` pair is
counted **exactly once** as ``scored`` XOR ``abstained`` — never both, never
dropped. When either the current or the future reference is missing, the pair
is *abstained* and the missing future reference is **never imputed** (CON-010):
we do not fabricate a fair value we did not observe.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from veridex.maker.contracts import Side, TargetQuoteSet
from veridex.maker.markout import forward_markout_bps

__all__ = ["QuoteMarkout", "QuoteAccounting", "score_r1_markout"]


class QuoteMarkout(BaseModel):
    """Quote-quality markout for one ``(quote, horizon)`` pair.

    Intentionally has **no** fill/PnL/executable-edge field: this measures how
    good the *quote* was relative to the moving reference, not what a fill would
    have earned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: int
    tick_seq: int
    side: Side
    market_key: str
    horizon_s: int
    markout_bps: int


class QuoteAccounting(BaseModel):
    """Full accounting for a scoring pass.

    ``scored + abstained`` equals the total number of ``(quote, horizon)`` pairs
    presented; ``excluded`` tracks any additional exclusion reasons (kept empty
    by the R1 pass, which only ever scores or abstains).
    """

    model_config = ConfigDict(extra="forbid")

    scored: int
    abstained: int
    excluded: dict[str, int] = Field(default_factory=dict)


def score_r1_markout(
    quote_sets: list[TargetQuoteSet],
    ref_at: Callable[[str, Side, int], float | None],
    horizons_s: tuple[int, ...],
) -> tuple[list[QuoteMarkout], QuoteAccounting]:
    """Score every quote's forward markout across the given horizons.

    For each quote in each quote set, and each horizon ``h``, look up the
    reference fair value now (at ``qs.ts``) and in the future (at ``qs.ts + h``).
    If either is missing, abstain (never impute the missing future reference,
    CON-010). Otherwise compute the forward markout and count it as scored.

    Args:
        quote_sets: Proposed target quote sets to score.
        ref_at: Reference lookup ``(market_key, side, ts) -> fair | None``.
        horizons_s: Forward horizons in seconds.

    Returns:
        A tuple of the scored :class:`QuoteMarkout` list and a
        :class:`QuoteAccounting` where every ``(quote, horizon)`` pair is
        counted exactly once as scored XOR abstained.
    """
    marks: list[QuoteMarkout] = []
    scored = 0
    abstained = 0

    for qs in quote_sets:
        for quote in qs.quotes:
            for h in horizons_s:
                ref_now = ref_at(quote.market_key, quote.side, qs.ts)
                ref_future = ref_at(quote.market_key, quote.side, qs.ts + h)
                if ref_now is None or ref_future is None:
                    abstained += 1
                    continue
                marks.append(
                    QuoteMarkout(
                        fixture_id=qs.fixture_id,
                        tick_seq=qs.tick_seq,
                        side=quote.side,
                        market_key=quote.market_key,
                        horizon_s=h,
                        markout_bps=forward_markout_bps(
                            side=quote.side,
                            quote_price=quote.price,
                            ref_now=ref_now,
                            ref_future=ref_future,
                        ),
                    )
                )
                scored += 1

    return marks, QuoteAccounting(scored=scored, abstained=abstained)
