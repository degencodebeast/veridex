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

import hashlib
import subprocess

import pytest

from tests.mm_strategy_ablation_harness import (
    arm_configs,
    load_base_config_overrides,
    load_tape,
    replay_arm,
)
from tests.test_dust_execution_integration import (
    _REPO_ROOT,
    _enumerate_sealed_json,
    _representative_directional_run,
)
from veridex.leaderboard import _rank_key as clv_key
from veridex.maker.leaderboard import maker_rank_key
from veridex.maker.result import assert_score_run_untouched
from veridex.rank_guards import R3_R4_RANK_DENYLIST, R4B_STRATEGY_DENYLIST_FIELDS
from veridex.scoring import _rank_key as dir_key
from veridex.scoring import score_run

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


# ============================================================================================
# SEC-004 / AC-003 / §6.3(7) / RED-30: sealed byte-identity AROUND the R4-B A/B harness.
# ============================================================================================
# Running the R4-B A/B ablation harness (BOTH arms) must leave the reviewed/sealed scoring
# artifacts BYTE-IDENTICAL: the enumerated E0-T1 sealed-JSON set AND a directional ``score_run``
# result are unchanged. The harness is TEST-SIDE — ``replay_arm`` seals + re-reads each arm's OWN
# live_recorder session under ``tmp_path`` and never opens the sealed maker JSONs nor the
# directional scorer — so the sealed set is untouched BY CONSTRUCTION; the SHA-vs-HEAD gate is the
# proof. R4-B carries a DISTINCT strategy id from the historical raw-FV ``TxLineFairMarketMakerAgent``,
# so it never overwrites that agent's sealed artifacts either.
#
# REUSE, not reinvention: the enumerated sealed set, the git-``HEAD`` SHA-256 loop, and the
# representative directional run come verbatim from the R4-A whole-lane gate
# (``tests/test_dust_execution_integration.py`` 450-559); this module drives the R4-B A/B harness
# in their place. The enumerated list is COMPUTED from the real committed tree (never a hardcoded
# wildcard), so a future glob/relocation regression cannot silently shrink the verified set.


def _run_r4b_ab_harness(tmp_path) -> None:
    """Drive the R4-B A/B ablation harness end-to-end: replay BOTH arms over the sealed tape.

    Mirrors the arm drivers in ``test_mm_strategy_ablation.py`` — build the shared A/B config pair
    and replay arm A (guard-off) and arm B (guard-on). Each arm seals + re-reads its OWN
    ``live_recorder`` session under ``tmp_path``; neither touches the sealed maker JSONs nor the
    directional scorer. Running this is what must leave the sealed set byte-identical.
    """
    arms = arm_configs(load_base_config_overrides())
    arm_a = replay_arm(load_tape("healthy"), arms.baseline, tmp_path / "r4b_arm_a")
    arm_b = replay_arm(load_tape("healthy"), arms.guarded, tmp_path / "r4b_arm_b")
    # non-vacuous: BOTH arms genuinely replayed (a real sealed run, not a no-op) before the check.
    assert arm_a.byte_reproduces and arm_b.byte_reproduces


def test_r4b_harness_leaves_sealed_json_byte_identical(tmp_path) -> None:
    """AC-003 / RED-30 / SEC-004: running the R4-B A/B harness leaves the enumerated E0-T1
    sealed-JSON set BYTE-IDENTICAL — each file's on-disk SHA-256 equals its committed ``HEAD`` SHA.

    The enumerated sealed set is COMPUTED from the real committed tree (reused verbatim from the
    R4-A whole-lane gate) and must include the sealed maker + directional-leaderboard fixtures the
    SEC-004 clause names, so a glob/relocation regression cannot silently shrink it.
    """
    sealed_files = _enumerate_sealed_json()
    # Anti-inert: the enumeration must carry EVERY known E0-T1 sealed output (the same required set
    # the R4-A gate asserts) — otherwise a shrunk set would let the SHA loop pass vacuously.
    required_sealed = {
        "scripts/txline_live/cp1/maker-arena-result.json",
        "contracts/fixtures/leaderboard.json",
        "contracts/fixtures/maker_arena_result.json",
    }
    missing = required_sealed - set(sealed_files)
    assert not missing, (
        f"enumerated sealed set is missing required sealed outputs {sorted(missing)}; got {sealed_files}"
    )

    _run_r4b_ab_harness(tmp_path)

    # EACH enumerated sealed file on disk is byte-identical to its committed HEAD content (SHA-256).
    for rel_path in sealed_files:
        on_disk = (_REPO_ROOT / rel_path).read_bytes()
        committed = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(committed).hexdigest(), (
            f"sealed file {rel_path} must be byte-identical to its committed HEAD content "
            "after the R4-B harness"
        )


def test_score_run_untouched_after_r4b(tmp_path) -> None:
    """SEC-004 / AC-003: after the R4-B A/B harness runs, a directional ``score_run`` result is
    byte-identical — :func:`assert_score_run_untouched` holds (the harness never mutates the scorer).
    """
    run = _representative_directional_run()
    before = score_run(run)

    _run_r4b_ab_harness(tmp_path)

    after = score_run(run)
    assert_score_run_untouched(before, after)  # raises iff before != after
