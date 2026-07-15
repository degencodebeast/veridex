"""SEC-005 / AC-033 / §6.3(6): seal R4-B strategy telemetry out of every rank surface.

R4-B is the experimental market-maker STRATEGY lane. Its per-arm diagnostic/telemetry
fields (matched-opportunity markout, exposure-normalized adverse selection, per-fill
markout, fill/abstention counts, capital-at-risk, arm id, decision reason codes) are
policy-experiment observations — they must NEVER enter the ranked leaderboard on ANY of
the three rank surfaces (directional CLV, cross-run CLV, maker toxicity).

This test mirrors the R4-A pattern in ``test_dust_execution_sec_isolation.py`` (263-301):
an INDEPENDENT literal (``EXPECTED_R4B_FIELDS``) is asserted EXACT-EQUAL to the named set
actually folded into the production denylist, and every field is exercised through all
three rank keys individually — INCLUDING the raw ``sorted(..., key=...)`` bypass path that
skips the per-row loop guard and hits the key function directly.
"""

from __future__ import annotations

import pytest

from veridex.leaderboard import _rank_key as clv_key
from veridex.maker.leaderboard import maker_rank_key
from veridex.rank_guards import R3_R4_RANK_DENYLIST, R4B_STRATEGY_DENYLIST_FIELDS
from veridex.scoring import _rank_key as dir_key

# GENUINELY INDEPENDENT literal — the ground-truth closed set of R4-B strategy telemetry
# field names (spec §6.3(6) / AC-033). NOT a copy of the production set: it is asserted
# exact-equal below, so any drift between this literal and the denylist FAILS (a dropped or
# renamed field cannot silently pass). Kept as a plain literal image (Fable-plan-review
# Minor-2: no "as applicable" — the set is closed and explicit).
EXPECTED_R4B_FIELDS = frozenset(
    {
        "matched_opportunity_markout",
        "exposure_normalized_adverse_selection",
        "per_fill_markout",
        "fill_count",
        "abstention_count",
        "capital_at_risk",
        "arm_id",
        "decision_reason_codes",
    }
)

# dir_key/clv_key SUBSCRIPT required metric keys, so a bare {poison} would raise KeyError,
# NOT the guard. Build a COMPLETE valid directional metrics row + the poisoned key so the
# rank guard (AssertionError) is the ONLY reason the call can raise. maker_rank_key uses
# ``.get(...)`` for its keys, so a bare {poison} row is a valid maker row + the poison.
VALID_DIR = {
    "avg_clv_bps": 0.0,
    "total_clv_bps": 0.0,
    "brier": 0.0,
    "max_drawdown": 0.0,
    "action_count": 0,
    "agent_id": "a",
}


def test_r4b_denylist_equals_expected_literal() -> None:
    """The independent literal EQUALS the R4-B set folded into the denylist (drift fails)."""
    # EXACT set-equality vs the named production set → omitting/renaming ANY field fails HERE.
    assert R4B_STRATEGY_DENYLIST_FIELDS == EXPECTED_R4B_FIELDS
    # …and the canonical rank denylist actually carries every one of those fields.
    assert EXPECTED_R4B_FIELDS <= R3_R4_RANK_DENYLIST


@pytest.mark.parametrize("field", sorted(EXPECTED_R4B_FIELDS))
def test_r4b_field_rejected_by_all_three_surfaces(field: str) -> None:
    """Every R4-B field is rejected by dir/clv/maker rank keys, incl. the raw sorted() bypass."""
    for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
        with pytest.raises(AssertionError):  # direct key call → the guard, not KeyError
            keyfn({**base, field: 1.0})
        with pytest.raises(AssertionError):  # raw sorted(..., key=...) bypass also raises
            sorted([{**base, field: 1.0}], key=keyfn)
