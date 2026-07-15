"""E6-T1: shared venue-only core + arm-identity + decision-parity ablation tests.

These are the honesty spine of the whole A/B claim (REQ-110 / AC-001 / AC-002 / RED-23 / RED-24):
ONE strategy core drives both arms, the guard is a SINGLE config-gated block, and the ONLY thing that
distinguishes arm A (``guard_enabled=False``) from arm B (``guard_enabled=True``) is that guard block.

The three named RED tests assert:
  * ``test_arm_identity_config_diff_is_guard_only`` (RED-24) — the config diff between the two arms is
    EXACTLY ``guard_enabled`` (field-level AND at the ``config_hash`` level once the guard is
    neutralized). Any other differing knob fails.
  * ``test_baseline_arm_consults_no_txline_state`` (RED-23) — arm A reads NO FV / TxLINE-derived state:
    its decision stream + state + observation hashes are byte-identical across healthy / stale / absent
    FV-health variants of the tape. This leans directly on the E3-T4 guard-off byte-identity contract.
  * ``test_replay_decision_parity`` (AC-001 / AC-002) — the SAME tape + config replayed twice yields a
    byte-identical decision stream (determinism; the clock is an EXPLICIT input, never wall-clock).

All machinery lives in the TEST-SIDE ``tests.mm_strategy_ablation_harness`` helper (never imported by
production / ranked code). The harness imports the veridex core to RUN the arms; it does not embed a
second copy of the policy.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from tests.mm_strategy_ablation_harness import (
    FIXTURES_DIR,
    FORBIDDEN_REACH_NULLS,
    FORBIDDEN_SINGLE_METRICS,
    HARNESS_MODULE_PATH,
    MARKOUT_REFERENCE,
    OBSERVED_MARKET_PRINT,
    OWN_RECONCILED_FILL,
    PERMITTED_CONCLUSION_SHAPE,
    PROMOTED_EVIDENCE_CLASSES,
    SIX_METRIC_KEYS,
    TRIGGER_REASON_CODES,
    AblationConclusion,
    ArmSingleMetrics,
    CounterfactualCapacityCeiling,
    ForbiddenReachNullError,
    MarkoutReferenceError,
    ObservedMarketPrint,
    RunReceipt,
    ablation_conclusion,
    arm_configs,
    arm_single_metrics,
    config_field_diff,
    counterfactual_capacity_ceiling,
    forbidden_import_hits,
    load_base_config_overrides,
    load_tape,
    matched_opportunity_report,
    mint_run_receipt,
    neutralize_guard,
    print_derived_trigger,
    replay_arm,
    reviewed_reach_baseline,
    venue_future_mid_series,
)
from veridex.mm_strategy.contracts import NeutralIntent, StrategyDecision
from veridex.mm_strategy.core import decide

# --- RED-24: arm identity — the config diff is the guard block, nothing else ----------------


def test_arm_identity_config_diff_is_guard_only() -> None:
    """The two arms differ in EXACTLY one knob — ``guard_enabled`` — at both the field and hash level.

    Both arms are derived from ONE shared base override set by flipping only ``guard_enabled``, so the
    config diff is structurally the guard block. The mutation (arm A given a different ``half_spread``)
    introduces a second differing field and this test goes red.
    """
    overrides = load_base_config_overrides()
    arms = arm_configs(overrides)

    # field-level: the ONLY differing knob is the guard block.
    assert config_field_diff(arms.baseline, arms.guarded) == {
        "guard_enabled": (False, True)
    }

    # hash-level: neutralize the guard block on both arms → identical canonical identity. If ANY other
    # knob differed (the mutation), these hashes diverge too.
    assert (
        neutralize_guard(arms.baseline).config_hash()
        == neutralize_guard(arms.guarded).config_hash()
    )

    # non-vacuous: the arms are genuinely distinct configs, and the guard flag is the real difference.
    assert arms.baseline.guard_enabled is False
    assert arms.guarded.guard_enabled is True
    assert arms.baseline.config_hash() != arms.guarded.config_hash()


# --- RED-23: the baseline arm consults NO FV / TxLINE-derived state --------------------------


def test_baseline_arm_consults_no_txline_state(tmp_path) -> None:
    """Arm A (guard-off) is byte-identical across FV health — it reads no FV/TxLINE state (E3-T4).

    Replay the SAME non-FV cadence with healthy / stale / absent FV under ``guard_enabled=False``: the
    observation hashes, decision stream, and final state hash are identical, ``guard_fv`` is ``None`` on
    every observation, and the state carries no ``guard_watermark``. The guarded arm is shown to DO vary
    with FV health, so this is a real projection, not a stream that ignores FV in both arms.
    """
    baseline = arm_configs(load_base_config_overrides()).baseline
    assert baseline.guard_enabled is False

    results = {
        health: replay_arm(load_tape(health), baseline, tmp_path / f"a_{health}")
        for health in ("absent", "healthy", "stale")
    }
    base = results["absent"]

    # non-vacuous: the baseline stream really minted three observations and trained venue accumulators.
    assert len(base.observations) == 3
    assert base.final_state.smoother_mid is not None

    for health in ("healthy", "stale"):
        result = results[health]
        assert result.observation_hashes == base.observation_hashes
        assert result.decisions == base.decisions
        assert result.decisions_digest == base.decisions_digest
        assert result.state_hash == base.state_hash
        # arm A carries NO fv value / ts / epoch ANYWHERE — observation OR state.
        assert all(obs.guard_fv is None for obs in result.observations)
        assert result.final_state.guard_watermark is None
        # the sealed on-disk tape replays byte-identically (read_session + replay_reproduces).
        assert result.byte_reproduces

    # non-vacuity contrast: with the guard ON, healthy vs absent FV yields a DIFFERENT stream.
    guarded = arm_configs(load_base_config_overrides()).guarded
    guarded_healthy = replay_arm(load_tape("healthy"), guarded, tmp_path / "b_healthy")
    guarded_absent = replay_arm(load_tape("absent"), guarded, tmp_path / "b_absent")
    assert guarded_healthy.observation_hashes != guarded_absent.observation_hashes
    assert any(obs.guard_fv is not None for obs in guarded_healthy.observations)


# --- AC-001 / AC-002: replay decision-parity (determinism, explicit clock) -------------------


def test_replay_decision_parity(tmp_path) -> None:
    """The SAME tape + config replayed twice → a byte-identical decision stream (AC-001 / AC-002).

    Determinism holds for BOTH arms. The two replays run at DIFFERENT wall-clock instants (a real sleep
    separates them): if ``decide`` consulted a wall-clock the streams would diverge — they do not,
    because the clock is an explicit per-observation input (``as_of_ts``), never read inside the core.
    The ``iter_change_series`` reconstruction from the sealed tape is asserted deterministic too.
    """
    overrides = load_base_config_overrides()
    for arm_name in ("baseline", "guarded"):
        config = getattr(arm_configs(overrides), arm_name)
        first = replay_arm(load_tape("healthy"), config, tmp_path / f"{arm_name}_1")
        time.sleep(0.02)  # advance the wall-clock between runs — AC-002 no-wall-clock probe.
        second = replay_arm(load_tape("healthy"), config, tmp_path / f"{arm_name}_2")

        assert first.decisions == second.decisions
        assert first.decisions_digest == second.decisions_digest
        assert first.state_hash == second.state_hash
        assert first.observation_hashes == second.observation_hashes
        # replay.py is genuinely consumed: the change-series reconstruction is deterministic, and the
        # sealed tape re-hashes to its recorded content hash on both runs.
        assert first.change_series == second.change_series
        assert first.byte_reproduces and second.byte_reproduces


# --- support: the harness is TEST-SIDE (no production / ranked import) ------------------------


def test_harness_is_test_side_no_ranked_import() -> None:
    """The ablation harness lives under ``tests/`` and imports no ranked / research / maker module.

    It may import the mm_strategy core + live_recorder replay to RUN the arms, but it must never be a
    production/ranked dependency nor pull a ranked lane in. A static scan of its source enforces this.
    """
    assert HARNESS_MODULE_PATH.parts[-2] == "tests"
    assert forbidden_import_hits() == []


# ============================================================================================
# E6-T2: divergence + quiescent + placebo controls (REQ-111 / AC-025 / RED-27)
# ============================================================================================
# Where E6-T1 proves the arms are IDENTICAL except the guard block, E6-T2 proves the guard block
# is LOAD-BEARING and HONEST: (1) on pinned adversarial tapes the guarded arm's decision stream MUST
# diverge from the FV-blind baseline (a side pull, an extreme abstain, a stale-FV NO_QUOTE); (2) on a
# no-trigger tape the guarded arm — genuinely live — changes NOTHING; (3) the markout methodology that
# would score whether the residual has edge is forward-NEXT-change only, never the near-circular
# same-move (a placebo that is anti-predictive forward but spuriously predictive same-move exposes it).
#
# A/B contrast axis — SUBSTANCE, never the identity stamp. Arm A and arm B ALWAYS carry distinct
# ``config_hash`` (``guard_enabled`` differs) and therefore distinct ``decision_id`` / causal hashes /
# ``client_order_id``s, so a full-object A/B compare is trivially unequal (and a full-object A/B match
# is impossible). The only meaningful comparison is the SUBSTANTIVE decision the guard actually shapes
# — kind + closed reason codes + priced legs — holding every other knob equal (REQ-110/111).


def _normalize_lineage(decisions):
    """Codex Gate#3 [B1]: a per-STREAM normalizer that strips arm-specific order ids from replacement
    lineage while preserving the RELATIONSHIP (which prior order a replace supersedes).

    Every ``client_order_id`` / ``replaces_client_order_id`` is a ``config_hash``-derived hash, so it
    necessarily differs between arm A and arm B even when the two arms make the SAME logical decision.
    We canonicalize each distinct order id to a positional token by first-appearance across the stream
    in decision/leg order — an ordering both arms share when their structure matches. A replace leg's
    old-order reference then normalizes to the token of the placed order it names (a low, shared index
    for a correct replacement) or to a fresh token if it names an order NEVER placed in this stream (a
    foreign/dangling lineage). Returns a callable mapping a raw ``replaces_client_order_id`` → its
    normalized token (or None when there is no lineage).
    """
    canonical: dict[str, int] = {}
    # Register every PLACED order first — these are the orders that exist in this stream, in order.
    for decision in decisions:
        for leg in decision.intent_plan:
            if leg.client_order_id is not None and leg.client_order_id not in canonical:
                canonical[leg.client_order_id] = len(canonical)

    def token(replaces_client_order_id):
        if replaces_client_order_id is None:
            return None
        if replaces_client_order_id not in canonical:
            # References an order never placed in this stream — a foreign/dangling lineage. It gets a
            # fresh token AFTER the placed orders, so it can never collide with a correct reference.
            canonical[replaces_client_order_id] = len(canonical)
        return canonical[replaces_client_order_id]

    return token


def _decision_signature(
    decision,
    lineage_token=lambda replaces_client_order_id: None,
) -> tuple[
    str, tuple[str, ...], tuple[tuple[str, str | None, float | None, bool, object], ...]
]:
    """The SUBSTANTIVE decision: kind + closed reason codes + priced legs
    (kind/side/price/post_only + normalized replacement lineage).

    EXCLUDES the per-arm identity stamp (``config_hash`` / ``decision_id`` / the four causal hashes /
    per-leg ``client_order_id``), which differs between arms purely because ``guard_enabled`` differs.
    Codex Gate#3 [B1]: it DOES include the replacement LINEAGE relationship — but only after the raw,
    arm-specific old-order id is normalized to a stream-positional token via ``lineage_token`` (see
    :func:`_normalize_lineage`). Comparing only kind/role/price/post_only let a replace that names the
    wrong/missing prior order appear quiescent; the normalized lineage token closes that gap while
    staying invariant to the arm-specific ids an honest A/B holds equal. The default token drops
    lineage entirely (None), so a stream with no replace legs — the whole current corpus — is
    unchanged. An honest guard A/B contrast is over exactly this substance.
    """
    legs = tuple(
        (
            leg.kind,
            leg.leg_role,
            leg.price,
            leg.post_only,
            lineage_token(leg.replaces_client_order_id),
        )
        for leg in decision.intent_plan
    )
    return (decision.kind, decision.reason_codes, legs)


def _control_overrides() -> dict[str, object]:
    """The shared A/B override set for the control tapes: the E6-T1 base plus SHORTENED warmups.

    ``ref_min_samples=2`` / ``basis_min_samples=2`` let a compact 4-tick canned tape warm BOTH the
    universal venue references AND the guarded basis, so the guard is genuinely live by the trigger
    frame. ``guard_enabled`` is still the ONLY knob :func:`arm_configs` flips between the arms — these
    knobs are applied IDENTICALLY to arm A and arm B (the E6-T1 arm-identity property is preserved).
    """
    return {**load_base_config_overrides(), "ref_min_samples": 2, "basis_min_samples": 2}


def test_guard_active_streams_differ(tmp_path) -> None:
    """On pinned adversarial tapes the guarded arm's decisions MUST diverge from the baseline (AC-025).

    Three divergence tapes, each: a byte-identical warmup prefix (both arms abstain while references /
    basis warm) then ONE trigger frame where the guard fires and the FV-blind baseline does not —
      * ``divergence_pull``    — residual inside ``(residual_band, extreme]`` → arm B pulls the adverse
                                 ASK (``QUOTE_ONE_SIDED`` / ``residual_pull_ask``, only the BID rests)
                                 while arm A quotes BOTH sides;
      * ``divergence_extreme`` — residual beyond ``extreme_multiple*residual_band`` → arm B fails closed
                                 (``NO_QUOTE`` / ``residual_extreme``) while arm A quotes;
      * ``divergence_stale``   — the projected FV is transport-stale → arm B fails closed BEFORE the
                                 residual (``NO_QUOTE`` / ``txline_stale``) while arm A quotes.
    The baseline is FV-blind throughout (no ``guard_fv`` on any observation — the E3-T4 spine); the
    guarded arm genuinely saw a fair value at the trigger. The divergence is EXACTLY the trigger frame
    (the warmup prefixes match) so it can only be the guard, never a warmup artefact.
    """
    arms = arm_configs(_control_overrides())
    # non-vacuity: the arms are genuinely distinct (guard on vs off) — a diverging stream must be the
    # guard doing work, and the two arms are a real A/B pair, not the same config twice.
    assert arms.baseline.guard_enabled is False
    assert arms.guarded.guard_enabled is True
    assert arms.baseline.config_hash() != arms.guarded.config_hash()

    for health, guarded_kind, guarded_reasons in (
        ("divergence_pull", "QUOTE_ONE_SIDED", ("residual_pull_ask",)),
        ("divergence_extreme", "NO_QUOTE", ("residual_extreme",)),
        ("divergence_stale", "NO_QUOTE", ("txline_stale",)),
    ):
        tape = load_tape(health)
        baseline = replay_arm(tape, arms.baseline, tmp_path / f"a_{health}")
        guarded = replay_arm(tape, arms.guarded, tmp_path / f"b_{health}")

        base_sig = [
            _decision_signature(d, _normalize_lineage(baseline.decisions))
            for d in baseline.decisions
        ]
        guard_sig = [
            _decision_signature(d, _normalize_lineage(guarded.decisions))
            for d in guarded.decisions
        ]

        # the streams DIFFER in substance — the guard changed behaviour (AC-025) ...
        assert base_sig != guard_sig, health
        # ... and ONLY on the trigger frame: every warmup decision is identical across arms, so the
        # divergence is the guard on the last tick and not an upstream (warmup) accident.
        assert base_sig[:-1] == guard_sig[:-1], health

        # trigger frame: arm A is FV-blind and quotes BOTH sides; arm B takes the guarded action.
        a_last, b_last = baseline.decisions[-1], guarded.decisions[-1]
        assert a_last.kind == "QUOTE_TWO_SIDED" and a_last.reason_codes == (), health
        assert b_last.kind == guarded_kind, health
        assert b_last.reason_codes == guarded_reasons, health

        # arm A consults NO FV anywhere (E3-T4 byte-identity spine); arm B really saw a fair value at
        # the trigger, so the divergence is a live guard decision, not a missing-input artefact.
        assert all(o.guard_fv is None for o in baseline.observations), health
        assert guarded.observations[-1].guard_fv is not None, health


def test_quiescent_streams_identical(tmp_path) -> None:
    """A no-trigger tape: the guarded arm — genuinely live — changes NOTHING vs the FV-blind baseline.

    The quiescent tape keeps the guard ON and FRESH (an FV on every tick, the basis warmed) but the FV
    AGREES with the venue mid (gap == basis ⇒ residual 0.0), so the guard fires on nothing and falls
    through to the SAME two-sided quote the baseline produces. Every decision's SUBSTANCE therefore
    matches across arms. This is the honesty counterweight to :func:`test_guard_active_streams_differ`:
    the guard diverges the stream ONLY when it actually fires, never as a blanket A/B artefact.
    """
    arms = arm_configs(_control_overrides())
    # the arms COULD differ — genuinely guard-on vs guard-off with distinct identity — so an identical
    # substance stream is a real no-op result, not a same-config tautology.
    assert arms.baseline.guard_enabled is False
    assert arms.guarded.guard_enabled is True
    assert arms.baseline.config_hash() != arms.guarded.config_hash()

    tape = load_tape("quiescent")
    baseline = replay_arm(tape, arms.baseline, tmp_path / "a")
    guarded = replay_arm(tape, arms.guarded, tmp_path / "b")

    base_sig = [
        _decision_signature(d, _normalize_lineage(baseline.decisions)) for d in baseline.decisions
    ]
    guard_sig = [
        _decision_signature(d, _normalize_lineage(guarded.decisions)) for d in guarded.decisions
    ]
    # NO trigger ⇒ the guarded and baseline decision streams are substance-IDENTICAL.
    assert base_sig == guard_sig

    # non-vacuity: the guarded arm was genuinely LIVE — it saw a fresh FV and actually quoted two-sided
    # on the row-H tick (a warmup-only tape would be a trivial 'both abstain' match, proving nothing).
    assert any(o.guard_fv is not None for o in guarded.observations)
    assert any(d.kind == "QUOTE_TWO_SIDED" for d in guarded.decisions)
    # ... and the arms are NOT byte-identical objects: their identity stamps (config_hash / decision_id)
    # differ, so the match is over decision SUBSTANCE — exactly what an honest A/B holds equal.
    assert guarded.decisions_digest != baseline.decisions_digest


# ============================================================================================
# Gate #4 F-IMPORTANT-2 (REQ-032 / REQ-070 rows F/W): basis warmup falls through to venue-only
# ============================================================================================
# During basis warmup the residual guard is INERT (REQ-032) and "the venue-only core still applies",
# so the guarded arm (B) must produce the SAME venue decision as the FV-blind baseline (A) on every
# matched opportunity — the A/B HONESTY FOUNDATION. AC-025's quiescent-control identity compares the
# :func:`_decision_signature` (kind + reason_codes + legs), so a spurious NO_QUOTE(basis_warmup) on a
# matched opportunity is a REAL divergence that inflates arm B's abstention count. The E6 control
# (``ref_min_samples == basis_min_samples == 2``) collapses to zero width the window where references
# are WARM but the basis is still COLD, hiding the divergence; these tests reopen it with
# ``basis_min_samples > ref_min_samples``.
#
# Two rows carry the inert-guard warmup:
#   * row H — guard on, refs warm, basis cold: the WHOLE guard block (FV-freshness + prematch + residual)
#     is bypassed → venue-only, signature == baseline (tests 1 and 5).
#   * row F — an ``fv_source_epoch`` increment clears the basis: dispositioned per its UNDERLYING row —
#     refs warm → venue-only (test 2), refs cold → NO_QUOTE(event_ref_warmup) (test 3).
# REQ-070 row F reads "per the underlying row, with the guard inert (``basis_warmup``)"; the controller
# ruling reads the parenthetical as NAMING the inert cause, not mandating NO_QUOTE — tests 2 and 3
# encode that interpretation, PENDING Codex confirmation in the re-review packet.

_WARMUP_TAPE = "quiescent"  # 4-tick FV-fresh no-trigger tape; mid 0.50, fv 0.55 (residual 0.0)


def _followon(template, *, observation_sequence, as_of_ts, fv_source_epoch=None, stale_fv=False):
    """Clone a warmed quiescent observation into a follow-on frame (book epoch + refs UNCHANGED).

    ``fv_source_epoch=None`` strips the guard leg — the guard-off/baseline frame. A guarded frame
    carries a FRESH FV unless ``stale_fv`` pushes ``fv_recv_ts`` past ``fv_freshness_ms``. Every
    ``recv_ts`` stays ``<= as_of_ts`` (REQ-022). Passing ``fv_source_epoch`` ABOVE the warmed
    watermark makes the frame an FV-epoch increment (row F); passing the SAME value keeps it on its
    underlying W/H row. The venue book / inventory are arm-identical, so cloning the guarded template
    and dropping the guard leg yields the byte-matching baseline frame.
    """
    fresh_recv = as_of_ts - 100
    update = {
        "observation_sequence": observation_sequence,
        "as_of_ts": as_of_ts,
        "book_recv_ts": fresh_recv,
        "match_state_recv_ts": fresh_recv,
        "market_status_recv_ts": fresh_recv,
        "inventory": template.inventory.model_copy(update={"projection_as_of_ts": as_of_ts}),
    }
    if fv_source_epoch is None:
        update["guard_fv"] = None
    else:
        update["guard_fv"] = template.guard_fv.model_copy(
            update={
                "fv_source_epoch": fv_source_epoch,
                # transport-stale ⇒ age > fv_freshness_ms (10 s); fresh ⇒ 300 ms old. Both <= as_of_ts.
                "fv_recv_ts": (as_of_ts - 20_000) if stale_fv else (as_of_ts - 300),
                "fv_source_ts": (as_of_ts // 1000) - 2,
            }
        )
    return template.model_copy(update=update)


def test_basis_warmup_quiescent_arms_identical(tmp_path) -> None:
    """RED — the A/B honesty foundation: with ``basis_min_samples > ref_min_samples`` a window exists
    where references are WARM but the basis is still COLD. REQ-032 makes the guard inert there and the
    venue-only core still applies, so the guarded arm must produce the SAME venue decision as the
    FV-blind baseline on every such matched opportunity.

    Currently FAILS: on the refs-warm/basis-cold frame the guarded arm returns NO_QUOTE(basis_warmup)
    while the baseline arm quotes two-sided — the spurious, non-guard abstention this fix removes.
    """
    overrides = {**load_base_config_overrides(), "ref_min_samples": 2, "basis_min_samples": 5}
    arms = arm_configs(overrides)
    baseline = replay_arm(load_tape(_WARMUP_TAPE), arms.baseline, tmp_path / "a")
    guarded = replay_arm(load_tape(_WARMUP_TAPE), arms.guarded, tmp_path / "b")

    base_sig = [_decision_signature(d) for d in baseline.decisions]
    guard_sig = [_decision_signature(d) for d in guarded.decisions]
    # whole-stream SUBSTANCE identity: during basis warmup the guard changes NOTHING (venue-only).
    assert guard_sig == base_sig

    # non-vacuity 1: the basis stayed COLD for the whole tape (never reached basis_min_samples), so the
    # identity is proven over the basis-warmup window — not a fully-warm fall-through where the guard
    # ran and found residual 0.0.
    assert guarded.final_state.basis_sample_count < overrides["basis_min_samples"]
    # non-vacuity 2: references DID warm — at least one frame is a genuine venue-only two-sided quote (a
    # matched opportunity), so the arms agree on SUBSTANCE, not on a blanket warmup abstention.
    window = [i for i, d in enumerate(baseline.decisions) if d.kind == "QUOTE_TWO_SIDED"]
    assert window, "expected >=1 refs-warm/basis-cold two-sided frame (the collapsed E6 window)"
    for i in window:
        assert guarded.decisions[i].kind == "QUOTE_TWO_SIDED"
        assert "basis_warmup" not in guarded.decisions[i].reason_codes
    # non-vacuity 3: the guarded arm genuinely saw a fresh FV (a live guard, not an absent-FV artefact).
    assert any(o.guard_fv is not None for o in guarded.observations)


def test_fv_epoch_reset_refs_warm_falls_through_to_venue_only(tmp_path) -> None:
    """RED — REQ-070 row F with WARM references (underlying row H): an ``fv_source_epoch`` increment
    (an FV reconnect) clears the basis, so the guard is inert and the frame is dispositioned per its
    underlying row — a venue-only two-sided quote, byte-identical in SUBSTANCE to the FV-blind baseline.

    Currently FAILS: the guarded arm returns NO_QUOTE(basis_warmup). (REQ-070 row-F interpretation per
    the controller ruling — the parenthetical ``basis_warmup`` names the inert cause, not the decision —
    PENDING Codex confirmation.)
    """
    arms = arm_configs(_control_overrides())  # ref_min=2, basis_min=2 → both arms fully warm post-tape
    warm_g = replay_arm(load_tape(_WARMUP_TAPE), arms.guarded, tmp_path / "g")
    warm_b = replay_arm(load_tape(_WARMUP_TAPE), arms.baseline, tmp_path / "b")
    template = warm_g.observations[0]
    seq = warm_g.final_state.last_observation_sequence + 1
    as_of = warm_g.final_state.last_as_of_ts + 10_000

    # FV reconnect: fv_source_epoch increments (book epoch UNCHANGED) → basis-only reset (row F).
    epoch = template.guard_fv.fv_source_epoch + 1
    guarded_frame = _followon(template, observation_sequence=seq, as_of_ts=as_of, fv_source_epoch=epoch)
    baseline_frame = _followon(template, observation_sequence=seq, as_of_ts=as_of)  # guard leg stripped

    g_decision, _ = decide(guarded_frame, warm_g.final_state, arms.guarded)
    b_decision, _ = decide(baseline_frame, warm_b.final_state, arms.baseline)

    # the baseline (row H, guard off) quotes two-sided; the guarded row-F frame must MATCH it.
    assert b_decision.kind == "QUOTE_TWO_SIDED" and b_decision.reason_codes == ()
    assert _decision_signature(g_decision) == _decision_signature(b_decision)
    # explicit: the guard is inert (venue-only), NOT a NO_QUOTE(basis_warmup) abstention.
    assert g_decision.kind == "QUOTE_TWO_SIDED"
    assert "basis_warmup" not in g_decision.reason_codes


def test_fv_epoch_reset_refs_cold_is_event_ref_warmup(tmp_path) -> None:
    """RED / Site-2 routing guard-rail — REQ-070 row F with COLD references (underlying row W): an
    ``fv_source_epoch`` increment while references are still below ``ref_min_samples`` dispositions per
    the underlying W row → NO_QUOTE(event_ref_warmup), NOT NO_QUOTE(basis_warmup) and NEVER a quote.

    Proves Site-2 routes row F to the CORRECT underlying row and the fall-through never fires while
    references are cold (row-W venue-reference warmup is a genuinely-unsafe quote, REQ-080). Currently
    FAILS: the guarded arm returns NO_QUOTE(basis_warmup).
    """
    # ref_min high so references never warm on the 4-tick tape; basis_min low so the guard watermark is
    # seeded (the FV-epoch increment is detectable) while the underlying row stays W, not H.
    overrides = {**load_base_config_overrides(), "ref_min_samples": 10, "basis_min_samples": 2}
    arms = arm_configs(overrides)
    warm_g = replay_arm(load_tape(_WARMUP_TAPE), arms.guarded, tmp_path / "g")
    template = warm_g.observations[0]
    seq = warm_g.final_state.last_observation_sequence + 1
    as_of = warm_g.final_state.last_as_of_ts + 10_000

    epoch = template.guard_fv.fv_source_epoch + 1  # FV-epoch increment (row F)
    frame = _followon(template, observation_sequence=seq, as_of_ts=as_of, fv_source_epoch=epoch)
    decision, _ = decide(frame, warm_g.final_state, arms.guarded)

    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("event_ref_warmup",)
    # non-vacuity: this genuinely was an FV-epoch frame (watermark seeded) with references below floor.
    assert warm_g.final_state.guard_watermark is not None
    assert len(warm_g.final_state.spread_ref_samples) < overrides["ref_min_samples"]


def test_venue_reference_warmup_still_blocks(tmp_path) -> None:
    """Guard-rail (Row W UNCHANGED): while references are COLD (below ``ref_min_samples``) every
    accepted frame is NO_QUOTE(event_ref_warmup) in BOTH arms — the basis-warmup fall-through NEVER
    fires while references are cold (that is venue-reference warmup, REQ-070 row W, where the
    event-protection gates are not yet evaluable so quoting is genuinely unsafe).

    Passes pre- AND post-fix; it goes red only if the fall-through mis-fires on a refs-cold frame.
    """
    overrides = {**load_base_config_overrides(), "ref_min_samples": 10, "basis_min_samples": 5}
    arms = arm_configs(overrides)
    baseline = replay_arm(load_tape(_WARMUP_TAPE), arms.baseline, tmp_path / "a")
    guarded = replay_arm(load_tape(_WARMUP_TAPE), arms.guarded, tmp_path / "b")

    assert [_decision_signature(d) for d in guarded.decisions] == [
        _decision_signature(d) for d in baseline.decisions
    ]
    # every guarded warmup frame is a venue-reference-warmup abstention — never a quote, never basis_warmup.
    assert all(d.kind != "QUOTE_TWO_SIDED" for d in guarded.decisions)
    assert all("basis_warmup" not in d.reason_codes for d in guarded.decisions)
    # non-vacuity: references really stayed COLD (smoother seeded, refs below floor) and ≥1 frame blocked.
    assert guarded.final_state.smoother_mid is not None
    assert len(guarded.final_state.spread_ref_samples) < overrides["ref_min_samples"]
    assert any(d.reason_codes == ("event_ref_warmup",) for d in guarded.decisions)


def test_stale_fv_during_basis_warmup_is_venue_only(tmp_path) -> None:
    """RED (reading-B proof) — the WHOLE guard, including the FV-freshness gate, is inert during basis
    warmup: guard ON, references WARM, basis COLD, FV transport-stale (no epoch change → underlying row
    H) → the guarded arm falls through to the venue-only decision, byte-identical to the FV-blind
    baseline — NOT NO_QUOTE(txline_stale).

    If the FV-freshness gate fired during basis warmup, a stale-FV frame would abstain on arm B while
    arm A quotes → a dishonest divergence attributed to a guard that cannot act anyway (no basis).
    Currently FAILS: the guarded arm returns NO_QUOTE(txline_stale) (the FV-freshness gate fires FIRST).
    """
    overrides = {**load_base_config_overrides(), "ref_min_samples": 2, "basis_min_samples": 10}
    arms = arm_configs(overrides)
    warm_g = replay_arm(load_tape(_WARMUP_TAPE), arms.guarded, tmp_path / "g")
    warm_b = replay_arm(load_tape(_WARMUP_TAPE), arms.baseline, tmp_path / "b")
    template = warm_g.observations[0]
    seq = warm_g.final_state.last_observation_sequence + 1
    as_of = warm_g.final_state.last_as_of_ts + 10_000

    # SAME fv epoch (no reset → underlying row H), but the FV leg is transport-stale.
    epoch = template.guard_fv.fv_source_epoch
    guarded_frame = _followon(
        template, observation_sequence=seq, as_of_ts=as_of, fv_source_epoch=epoch, stale_fv=True
    )
    baseline_frame = _followon(template, observation_sequence=seq, as_of_ts=as_of)

    g_decision, _ = decide(guarded_frame, warm_g.final_state, arms.guarded)
    b_decision, _ = decide(baseline_frame, warm_b.final_state, arms.baseline)

    assert b_decision.kind == "QUOTE_TWO_SIDED"
    assert _decision_signature(g_decision) == _decision_signature(b_decision)
    assert "txline_stale" not in g_decision.reason_codes
    # non-vacuity: the basis really is still below the floor (cold) at this frame.
    assert warm_g.final_state.basis_sample_count < overrides["basis_min_samples"]


def _place_decision(client_order_id: str) -> StrategyDecision:
    """A tick that PLACES one bid — the order a later tick may replace (arm-specific coid)."""
    return StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(
            NeutralIntent(
                kind="place_quote", leg_role="bid", price=0.49, client_order_id=client_order_id
            ),
        ),
    )


def _replace_decision(client_order_id: str, replaces_client_order_id: str) -> StrategyDecision:
    """A tick that REPLACES a prior resting bid, naming the exact old order (arm-specific coids)."""
    return StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(
            NeutralIntent(
                kind="replace_quote",
                leg_role="bid",
                price=0.50,
                client_order_id=client_order_id,
                replaces_client_order_id=replaces_client_order_id,
            ),
        ),
    )


def test_ab_signature_compares_lineage_normalized() -> None:
    """Codex Gate#3 [B1]: the A/B decision signature must compare replacement LINEAGE, after
    normalizing the arm-specific (``config_hash``-derived) order ids out.

    A replace leg's ``client_order_id`` / ``replaces_client_order_id`` differ between arms purely
    because ``decision_id`` (hence the hashed ids) differs by ``guard_enabled`` — an honest A/B holds
    that identity stamp equal. But the lineage RELATIONSHIP (which prior order the replace supersedes)
    is substantive: a replace that names the wrong/foreign old order is a real behavioural divergence,
    NOT quiescence. So the signature must (a) treat two arms whose replace names the SAME logical prior
    placement as identical, yet (b) diverge when one arm's replace names a foreign order never placed.
    Comparing only kind/role/price/post_only (the pre-[B1] signature) would let that divergence hide.
    """
    # Arm A: place order X, then replace X (correct lineage). Ids are arm-A-specific hashes.
    arm_a = [_place_decision("A-x"), _replace_decision("A-y", replaces_client_order_id="A-x")]
    # Arm B (correct): the SAME structure with arm-B-specific ids — replace names the placed order.
    arm_b_ok = [_place_decision("B-x"), _replace_decision("B-y", replaces_client_order_id="B-x")]
    # Arm B (wrong): the replace names a FOREIGN order never placed in this stream — a real divergence
    # that only lineage comparison can see (kind/role/price/post_only are byte-identical to arm A).
    arm_b_bad = [
        _place_decision("B-x"),
        _replace_decision("B-y", replaces_client_order_id="B-foreign"),
    ]

    def sig(decisions: list[StrategyDecision]) -> list[object]:
        token = _normalize_lineage(decisions)
        return [_decision_signature(d, token) for d in decisions]

    # (a) arm-specific ids normalized OUT: correct lineage in both arms → IDENTICAL signatures.
    assert sig(arm_a) == sig(arm_b_ok)
    # (b) a foreign/wrong old-order reference is NOT quiescent — the normalized signature diverges.
    assert sig(arm_a) != sig(arm_b_bad)

    # Non-vacuity: the divergence is EXACTLY the replace tick's lineage — the place tick still matches.
    assert sig(arm_a)[0] == sig(arm_b_bad)[0]
    assert sig(arm_a)[1] != sig(arm_b_bad)[1]


# --- placebo: forward-NEXT-change markout methodology (never the near-circular same-move) -----


def _load_placebo_series() -> list[tuple[float, float]]:
    """Load the pinned placebo series as ``[(residual, mid), ...]`` (offline, deterministic)."""
    payload = json.loads((FIXTURES_DIR / "placebo_series.json").read_text())
    return [(float(s["residual"]), float(s["mid"])) for s in payload["samples"]]


def _sign(value: float) -> int:
    """The three-valued sign of ``value`` (``-1`` / ``0`` / ``+1``)."""
    return (value > 0.0) - (value < 0.0)


def _directional_edge(samples: list[tuple[float, float]], *, horizon: str) -> float:
    """Mean directional agreement of each residual sign with a venue move, over the chosen horizon.

    This is the markout METHODOLOGY under test (REQ-111 / RED-27):
      * ``horizon="next"`` — score ``residual[i]`` against the FORWARD next change
        ``mid[i+1] - mid[i]``. This is the ONLY honest markout horizon: a signal earns edge by
        anticipating the venue move that has NOT happened yet. Scored over ``i in [0, n-1)``.
      * ``horizon="same"`` — score ``residual[i]`` against the CONTEMPORANEOUS change
        ``mid[i] - mid[i-1]`` — the move that PRODUCED ``residual[i]``. This is near-circular and is
        FORBIDDEN as an edge measure; it exists here only so the placebo can expose it. Scored over
        ``i in [1, n)``.

    Returns the mean of ``sign(residual) * sign(change)``: ``+1.0`` perfectly predictive, ``0.0`` none,
    ``-1.0`` perfectly anti-predictive.
    """
    hits: list[int] = []
    if horizon == "next":
        for i in range(len(samples) - 1):
            change = samples[i + 1][1] - samples[i][1]
            hits.append(_sign(samples[i][0]) * _sign(change))
    elif horizon == "same":
        for i in range(1, len(samples)):
            change = samples[i][1] - samples[i - 1][1]
            hits.append(_sign(samples[i][0]) * _sign(change))
    else:
        raise ValueError(f"unknown horizon {horizon!r}")
    return sum(hits) / len(hits)


def test_placebo_anti_predictive_next_change_only() -> None:
    """The placebo residual has NO forward edge — honest markout is the NEXT change, never same-move.

    The pinned placebo series is engineered so the residual is anti-predictive of the FORWARD next
    change yet perfectly aligned with the SAME (contemporaneous) change that produced it. Scoring on
    the honest forward horizon reveals no edge (in fact anti); scoring on the near-circular same move
    would spuriously report a perfect edge on the SAME data (RED-27).

    Teeth: mutate :func:`_directional_edge`'s ``horizon="next"`` branch to score the same move instead
    (the forbidden circular measure) and ``forward`` jumps to the same ``+1.0`` — both assertions below
    fail. Scoring the forward next change is exactly what defeats the placebo.
    """
    samples = _load_placebo_series()
    forward = _directional_edge(samples, horizon="next")
    same = _directional_edge(samples, horizon="same")

    # HONEST forward-NEXT-change scoring finds NO predictive edge (the placebo is anti-predictive).
    assert forward <= 0.0
    # Non-vacuity + circularity exposure: the near-circular SAME-move scoring spuriously reports a
    # strong edge on the SAME series — so a methodology that scored the same move would FALSELY promote
    # this placebo, and the forward-only assertion above is what keeps it honest.
    assert same > 0.0
    assert forward < same


# ============================================================================================
# E6-T3: six-metric matched-opportunity report + venue-derived reference (REQ-112/113/AC-027/RED-26)
# ============================================================================================
# E6-T1 proves the arms are IDENTICAL except the guard block; E6-T2 proves the guard is LOAD-BEARING and
# the markout methodology is forward-only. E6-T3 pins the EVALUATION REPORT that would score the arms:
# the SIX mandatory metrics reported ALWAYS TOGETHER (no favorable subset alone), each markout scored
# against a VENUE-derived reference — the venue's OWN future mid at the next venue change (event-time) —
# NEVER the TxLINE FV the guard consumes. Scoring the FV-driven guard against the SAME FV is circular
# self-validation (a guard that merely parrots FV would look perfect), so an FV-referenced markout FAILS
# CLOSED (REQ-113/RED-26). The report runs on the ``markout`` tape: both arms warm and quote two-sided
# while the venue mid STEPS (so an event-time markout horizon exists), then the guarded arm fails closed
# to NO_QUOTE on the extreme-residual tick (a real abstention feeding the abstention metric).


def _sign_for(leg_role: str) -> int:
    """Markout sign by side — a bid (long) gains when the venue rises; an ask (short) when it falls."""
    return 1 if leg_role == "bid" else -1


def test_six_metrics_reported_together(tmp_path) -> None:
    """The evaluation report carries ALL SIX mandatory metrics together — no favorable subset (AC-027).

    ``metrics()`` returns EXACTLY the pinned six-metric set, so a flattering number (better per-fill
    markout, fewer fills) can never be exposed on its own — it always travels with the honest
    denominators (matched-opportunity delta, abstention count, capital at risk). The tape is exercised
    for real: the guarded arm posts candidate fills against stepping venue mids AND abstains once, so
    every metric is non-vacuous, not a hard-coded zero.
    """
    arms = arm_configs(_control_overrides())
    tape = load_tape("markout")
    baseline = replay_arm(tape, arms.baseline, tmp_path / "a")
    guarded = replay_arm(tape, arms.guarded, tmp_path / "b")

    report = matched_opportunity_report(baseline, guarded)

    # the pinned six-metric set is EXACTLY these keys — the report cannot emit a favorable subset alone.
    assert frozenset(
        {
            "per_fill_markout",
            "matched_opportunity_markout",
            "exposure_normalized_adverse_selection",
            "fill_count",
            "abstention_count",
            "capital_at_risk",
        }
    ) == SIX_METRIC_KEYS
    assert set(report.metrics()) == SIX_METRIC_KEYS
    for key in SIX_METRIC_KEYS:
        assert report.metrics()[key] is not None, key

    # non-vacuity: the naive favorable metric (per-fill markout) is reported WITH real honest
    # denominators — genuine fills, at least one abstention, capital genuinely at risk, and a non-empty
    # MATCHED (paired A-vs-B over the SAME eligible opportunities) set.
    assert report.fill_count > 0
    assert report.abstention_count >= 1
    assert report.capital_at_risk > 0.0
    assert report.matched_opportunity_count > 0
    # internal consistency: the fill count is exactly the candidate-fill detail the report exposes, and
    # per-fill markout is their mean — no favorable-subset filtering hidden inside.
    assert report.fill_count == len(report.fills)
    assert report.metrics()["per_fill_markout"] == pytest.approx(
        sum(f.markout_bps for f in report.fills) / len(report.fills)
    )


def test_markout_reference_is_venue_not_fv(tmp_path) -> None:
    """The markout reference is the VENUE future mid (event-time), never the FV — FV fails closed (RED-26).

    Scoring the FV-driven guard against the SAME FV is circular self-validation. Every fill is scored
    against the venue's OWN future mid at the next venue change; an FV-referenced markout is REFUSED
    (``MarkoutReferenceError``) rather than computed. On this tape the FV (mid+0.05) and the venue mid
    genuinely DISAGREE, so 'venue-derived' is a real distinction: recomputing the SAME fills against the
    FV yields a DIFFERENT mean. The pinned reference is ``"venue"``.
    """
    arms = arm_configs(_control_overrides())
    tape = load_tape("markout")
    baseline = replay_arm(tape, arms.baseline, tmp_path / "a")
    guarded = replay_arm(tape, arms.guarded, tmp_path / "b")

    report = matched_opportunity_report(baseline, guarded)
    assert MARKOUT_REFERENCE == "venue"
    assert report.markout_reference == "venue"

    # the venue future-mid series is EVENT-TIME (the NEXT venue change), not a fixed wall-clock offset.
    venue_future = venue_future_mid_series(guarded.observations)
    assert any(v is not None for v in venue_future)

    # per-fill markout is the VENUE-referenced mean over the SAME candidate fills the report exposes.
    assert report.per_fill_markout == pytest.approx(
        sum(f.markout_bps for f in report.fills) / len(report.fills)
    )

    # PER-FILL, the score is the venue's OWN future mid, never the FV the guard consumed. The FV
    # genuinely DISAGREES with the venue mid on this tape (fv = mid+0.05), so re-scoring the SAME fill
    # against the FV-at-horizon yields a DIFFERENT number — proof the report is not vacuously the FV.
    assert report.fills  # there are candidate fills to score
    for fill in report.fills:
        horizon_fv = guarded.observations[fill.horizon_index].guard_fv
        assert horizon_fv is not None  # the guarded arm saw a fair value at every scored horizon
        assert horizon_fv.fv != fill.venue_future_mid  # FV and venue disagree at every scored fill
        venue_markout = round(
            _sign_for(fill.leg_role)
            * (fill.venue_future_mid - fill.quote_price)
            / fill.venue_now
            * 1e4
        )
        fv_markout = round(
            _sign_for(fill.leg_role)
            * (horizon_fv.fv - fill.quote_price)
            / fill.venue_now
            * 1e4
        )
        assert fill.markout_bps == venue_markout  # the report scored against the VENUE future mid ...
        assert fill.markout_bps != fv_markout  # ... NOT the circular FV reference.

    # an FV-referenced markout is REFUSED — the harness fails closed rather than compute a circular score.
    with pytest.raises(MarkoutReferenceError):
        matched_opportunity_report(baseline, guarded, reference="fv")


# --- RED-25 / AC-026: forbidden-comparison guard — "fewer trades ≠ better" -------------------
# REQ-114 names three forbidden comparisons: total-PnL-alone, fill-count-alone ("fewer trades ≠
# better"), per-fill-markout-alone. NONE may become a benefit/"better" verdict; the ONLY permitted
# conclusion is a matched-opportunity risk-edge HYPOTHESIS, pending Gate B.


def _adversarial_favorable_arm_pair() -> tuple[ArmSingleMetrics, ArmSingleMetrics]:
    """A (baseline, guarded) single-metric pair where arm B trades LESS with lower total loss.

    The exact REQ-114 forbidden-favorable case: arm B posts FEWER fills (3 < 12), carries a LOWER total
    loss (-40 vs -900 bps, i.e. less negative), AND a better per-fill markout. EVERY single metric points
    "B looks better" — precisely the temptation the forbidden-comparison guard must refuse. The values are
    an explicit adversarial construction (not a replay artefact) because the frozen fixtures never post a
    losing arm; the point is the conclusion logic's refusal, exercised against the worst-case flattery.
    """
    baseline = ArmSingleMetrics(total_markout_bps=-900.0, fill_count=12, per_fill_markout=-75.0)
    guarded = ArmSingleMetrics(total_markout_bps=-40.0, fill_count=3, per_fill_markout=-13.3)
    return baseline, guarded


def test_fewer_trades_lower_loss_no_benefit_inference(tmp_path) -> None:
    """An arm trading LESS with lower total loss is NOT "better" — only the matched hypothesis survives.

    REQ-114 forbids three comparisons by name: total-PnL-alone, fill-count-alone ("fewer trades ≠
    better"), per-fill-markout-alone. Arm B here trades fewer fills, carries a lower total loss, AND a
    better per-fill markout — every single metric flatters it — yet the ONLY conclusion the guard yields
    is the matched-opportunity risk-edge HYPOTHESIS pending Gate B: no single favorable metric becomes a
    benefit verdict. The six-metric report feeding the conclusion is a REAL replay (non-vacuous).
    """
    # the three forbidden-alone comparisons are pinned by name (REQ-114).
    assert frozenset(
        {"total_pnl", "fill_count", "per_fill_markout"}
    ) == FORBIDDEN_SINGLE_METRICS

    arms = arm_configs(_control_overrides())
    tape = load_tape("markout")
    baseline_run = replay_arm(tape, arms.baseline, tmp_path / "a")
    guarded_run = replay_arm(tape, arms.guarded, tmp_path / "b")
    report = matched_opportunity_report(baseline_run, guarded_run)

    baseline_metrics, guarded_metrics = _adversarial_favorable_arm_pair()

    # the scenario really is the forbidden-favorable one: B trades LESS, lower total loss, better per-fill.
    assert guarded_metrics.fill_count < baseline_metrics.fill_count  # fewer trades
    assert guarded_metrics.total_markout_bps > baseline_metrics.total_markout_bps  # lower total loss
    assert guarded_metrics.per_fill_markout > baseline_metrics.per_fill_markout  # better per-fill

    conclusion = ablation_conclusion(report, baseline_metrics, guarded_metrics)

    # the ONLY permitted conclusion shape — a matched-opportunity risk-edge HYPOTHESIS pending Gate B.
    assert conclusion.shape == PERMITTED_CONCLUSION_SHAPE
    assert "hypothesis" in PERMITTED_CONCLUSION_SHAPE
    assert "matched opportunities" in PERMITTED_CONCLUSION_SHAPE
    assert "pending Gate B" in PERMITTED_CONCLUSION_SHAPE
    assert conclusion.hypothesis_only is True
    assert conclusion.gate_b_pending is True

    # THE GUARD: despite fewer trades + lower total loss + better per-fill markout, NO benefit is inferred.
    assert conclusion.infers_benefit() is False

    # the forbidden single-metric deltas are RECORDED (an honest packet) but NON-conclusive — every one
    # points "B better", yet none moves the verdict off the pending-Gate-B hypothesis.
    assert conclusion.total_markout_delta > 0.0  # B's lower total loss is visible ...
    assert conclusion.fill_count_delta < 0  # ... and its fewer trades ...
    assert conclusion.per_fill_markout_delta > 0.0  # ... and its better per-fill markout ...
    # ... yet AblationConclusion carries NO benefit/better/winner verdict field — forbidden by shape.
    verdict_terms = ("benefit", "better", "winner", "beats", "superior")
    assert not any(
        term in field_name
        for field_name in AblationConclusion.__dataclass_fields__
        for term in verdict_terms
    )
    assert not any(term in PERMITTED_CONCLUSION_SHAPE.lower() for term in verdict_terms)

    # the permitted conclusion is fed by the REAL matched-opportunity signal — the honest paired delta
    # over the SAME eligible opportunities — never by any single arm-total metric.
    assert conclusion.matched_opportunity_markout == report.matched_opportunity_markout

    # exercised on the REAL arm pair too: reading each arm's OWN single metrics still yields no benefit
    # verdict — only the matched-opportunity hypothesis pending Gate B.
    real_conclusion = ablation_conclusion(
        report,
        arm_single_metrics(baseline_run),
        arm_single_metrics(guarded_run),
    )
    assert real_conclusion.shape == PERMITTED_CONCLUSION_SHAPE
    assert real_conclusion.infers_benefit() is False


# ============================================================================================
# E6-T5: print / reach / label honesty guards (REQ-115 / HON-002/004/005 / AC-028 / RED-28)
# ============================================================================================
# E6-T1..T4 pin the ablation spine, the load-bearing guard, and the six-metric evaluation. E6-T5 pins
# the Gate B EXECUTION-EVIDENCE labeling seam that sits UNDER all of it: a third-party trade print is an
# OBSERVED_MARKET_PRINT — an OBSERVATION of the market, NEVER our own fill / PnL / capacity / arrival
# order (REQ-026/027/052). Three honesty seams, each made STRUCTURAL by the harness (not a runtime
# check that could be bypassed):
#   (1) our book-arrival timing is kept SEPARATE from third-party-print timing — conflating them would
#       let a print masquerade as our own book event;
#   (2) a raw reach % / a single print is structurally NOT a member of the closed REQ-080 venue-book
#       trigger vocabulary (contracts.ReasonCode) and NEVER becomes our fill or feeds PnL;
#   (3) counterfactual capacity is a RECOMPUTED CEILING (an upper bound from observed prints), never
#       our realized fill / PnL / capacity; and a reach scored against a forbidden universal null
#       (0.5, p/b) can never assert edge (REQ-054 / HON-004).
# Private practitioner material (the privacy-locked quote/level-age anecdote) stays INTERNAL — it is
# never surfaced as a field on the public diagnostic (HON-005).


def test_raw_reach_or_print_cannot_become_trigger_or_fill() -> None:
    """A raw reach % / a single third-party print is a diagnostic — NEVER a live trigger or our fill.

    The AC-028 / RED-28 guard. A third-party trade print is an ``OBSERVED_MARKET_PRINT`` (Gate B
    execution-evidence CEILING; REQ-026): an OBSERVATION of the market, not our own fill. This test
    asserts, on a print observed OFFLINE:
      * (label + timing) it wears the ``OBSERVED_MARKET_PRINT`` label — never ``OWN_RECONCILED_FILL`` —
        and its print-observation timing is kept SEPARATE from our own book-arrival timing (distinct
        fields, distinct values), so a print can never masquerade as our book event;
      * (a) NO TRIGGER — neither the print nor its reach % is a member of the closed REQ-080 venue-book
        trigger set, and :func:`print_derived_trigger` yields ``None``: a reach %/print cannot pull a
        live quote;
      * (b) NOT OUR FILL / NO PnL / NOT OUR CAPACITY — a print is never counted as our fill, never feeds
        PnL, and the only capacity it informs is a RECOMPUTED counterfactual CEILING, never our capacity
        (REQ-027).

    Teeth (RED-28 mutation): let a reach % feed a quote trigger — return a real ``ReasonCode`` from
    :func:`print_derived_trigger` keyed on ``mark.reach_fraction`` — and the ``print_derived_trigger(...)
    is None`` assertion in branch (a) goes red. Scoring a reach %/print as a diagnostic (never a trigger)
    is exactly what keeps it out of the quote decision.
    """
    # A third-party trade print, observed OFFLINE. Its print-observation timestamp is DISTINCT from our
    # own book-arrival timestamp — a print is not our book event.
    mark = ObservedMarketPrint(
        price=0.53,
        size=250.0,
        print_recv_ts=1_000,
        book_arrival_ts=1_050,
        reach_fraction=0.42,
    )

    # (label) the print wears the Gate B ceiling label, never our-fill label.
    assert mark.label == OBSERVED_MARKET_PRINT
    assert mark.label != OWN_RECONCILED_FILL

    # (timing) book-arrival vs third-party-print timing are SEPARATED — distinct fields AND values, so a
    # third-party print can never be read as our own book event.
    assert mark.book_arrival_ts != mark.print_recv_ts

    # (a) NO TRIGGER: the print-derived trigger is None, and neither the print label nor its raw reach %
    # is a member of the closed REQ-080 venue-book trigger vocabulary — a reach %/print cannot pull a
    # live quote. This is the assertion the RED-28 mutation (a reach % feeding a trigger) breaks.
    assert print_derived_trigger(mark) is None
    # non-vacuity: the trigger set is genuinely the core's closed vocabulary (a REAL venue-book trigger
    # IS a member) yet a raw reach %/print label is structurally NOT.
    assert TRIGGER_REASON_CODES
    assert "book_thin" in TRIGGER_REASON_CODES
    assert OBSERVED_MARKET_PRINT not in TRIGGER_REASON_CODES
    assert str(mark.reach_fraction) not in TRIGGER_REASON_CODES
    assert "reach" not in "".join(TRIGGER_REASON_CODES)

    # (b) NOT OUR FILL / NO PnL: a print is an observation, never counted as our fill or fed to PnL.
    assert mark.is_our_fill() is False
    assert mark.pnl_contribution() == 0.0

    # (b, capacity) the ONLY capacity a print informs is a RECOMPUTED counterfactual CEILING — an upper
    # bound from the observed print sizes, explicitly NOT our realized fill / PnL / capacity (REQ-027).
    ceiling = counterfactual_capacity_ceiling((mark,))
    assert isinstance(ceiling, CounterfactualCapacityCeiling)
    assert ceiling.is_ceiling is True
    assert ceiling.is_our_capacity is False
    assert ceiling.label == OBSERVED_MARKET_PRINT
    # it is a genuine recompute from the observed prints (the honest ceiling), not a stored our-fill
    # number: two prints raise the ceiling to their summed size.
    other = ObservedMarketPrint(
        price=0.54,
        size=100.0,
        print_recv_ts=1_200,
        book_arrival_ts=1_260,
        reach_fraction=0.31,
    )
    assert counterfactual_capacity_ceiling((mark, other)).ceiling_size == pytest.approx(350.0)


def test_reach_baseline_rejects_forbidden_universal_null() -> None:
    """A reach baseline may not be a forbidden UNIVERSAL null (``0.5`` / ``p/b``); private notes stay in.

    REQ-054 / HON-004: a raw ``0.5`` and the naive price/book (``p/b``) reach are FORBIDDEN as a reach
    baseline — a reach scored against them can never assert edge, so :func:`reviewed_reach_baseline`
    fails closed on them. This is a SCOPED negative (exactly those two nulls), NOT a universal ban on
    reach as a diagnostic: a reviewed baseline passes through unchanged (HON-004). Separately, HON-005
    pins that private practitioner material (the privacy-locked quote/level-age anecdote) stays INTERNAL
    — it is never a field on the public ``OBSERVED_MARKET_PRINT`` diagnostic.
    """
    # the two forbidden universal nulls are pinned by name (REQ-054).
    assert frozenset({"0.5", "p/b"}) == FORBIDDEN_REACH_NULLS

    # each forbidden null FAILS CLOSED — the harness refuses to treat it as an edge baseline.
    for forbidden in ("0.5", "p/b"):
        with pytest.raises(ForbiddenReachNullError):
            reviewed_reach_baseline(forbidden)

    # HON-004 scope: reach is NOT universally banned — a reviewed, dependence-preserving null passes
    # through unchanged, so a reach diagnostic itself is still permitted.
    reviewed = "matched-dependence-preserving-null"
    assert reviewed_reach_baseline(reviewed) == reviewed

    # HON-005: no private practitioner material (quote/level-age anecdote) is surfaced as a public field
    # on the diagnostic — it stays internal. A field carrying it would be a leak.
    private_tokens = ("practitioner", "private", "anecdote", "quote_age", "level_age")
    for field_name in ObservedMarketPrint.__dataclass_fields__:
        assert not any(token in field_name for token in private_tokens), field_name
    for field_name in CounterfactualCapacityCeiling.__dataclass_fields__:
        assert not any(token in field_name for token in private_tokens), field_name


# --- E6-T6: run-receipt labels/hashes + relabel-fails-closed + historical reproduction -------
# REQ-116 (run-receipt provenance + labels), REQ-043(H42) (evidence-class gating: Gate-B OPEN/STALE ⇒
# EXPERIMENTAL_DUST), AC-029 / AC-030 / RED-29 / RED-31. Three seams: (1) a request-metadata relabel to a
# promotion class fails closed — the receipt stays EXPERIMENTAL_DUST; (2) a later config revision gets a
# new config_hash but historical decisions reproduce byte-identically under their originally-pinned
# config; (3) no ``veridex.research`` import anywhere in the harness/receipt or test source (AST scan).

# The four mandatory honesty labels a dust run receipt PINS (REQ-116) — reused verbatim from the pinned
# production honesty surface (``veridex.mm_strategy.contracts``), asserted here so a receipt can never
# silently drift from them.
_EXPECTED_RECEIPT_LABELS = {
    "evidence_class": "EXPERIMENTAL_DUST",
    "run_label": "DUST_LIVE",
    "calibration_label": "UNCALIBRATED",
    "edge_label": "NOT_PROVEN_EDGE",
}


@pytest.mark.parametrize("gate_b_status", ["OPEN", "STALE"])
@pytest.mark.parametrize("requested", ["PROMOTED", "EVIDENCE_GATED"])
def test_gate_b_open_metadata_relabel_stays_experimental_dust(
    tmp_path, gate_b_status: str, requested: str
) -> None:
    """A request-metadata relabel to a promotion class FAILS CLOSED under Gate-B OPEN/STALE (AC-029/RED-29).

    R4-B proves SAFETY, not alpha: there is NO code path that promotes the evidence class, so under a
    Gate-B status of OPEN or STALE an untrusted request asking to relabel the run ``PROMOTED`` /
    ``EVIDENCE_GATED`` has ZERO effect — the minted receipt stays ``EXPERIMENTAL_DUST`` and carries the
    full pinned provenance (strategy id/revision, config hash, per-observation hashes, the linked
    state-hash chain, decision ids, Gate-B evidence revision consumed) + the four honesty labels. The
    RED-29 mutation honors ``requested_evidence_class`` inside ``mint_run_receipt``, routing the relabel
    through so ``evidence_class`` becomes the requested promotion class and this test goes red.
    """
    # the two promotion classes an R4-B receipt can never wear are pinned by name (REQ-043(H42)).
    assert frozenset({"PROMOTED", "EVIDENCE_GATED"}) == PROMOTED_EVIDENCE_CLASSES
    assert requested in PROMOTED_EVIDENCE_CLASSES

    tape = load_tape("healthy")
    config = arm_configs(load_base_config_overrides()).guarded
    result = replay_arm(tape, config, tmp_path / "arm")

    receipt = mint_run_receipt(
        result,
        config,
        gate_b_status=gate_b_status,
        gate_b_evidence_revision="gate-b-rev-1",
        requested_evidence_class=requested,
    )

    # fail closed: the relabel does NOT take effect — the evidence class stays EXPERIMENTAL_DUST.
    assert isinstance(receipt, RunReceipt)
    assert receipt.evidence_class == "EXPERIMENTAL_DUST"
    assert receipt.evidence_class not in PROMOTED_EVIDENCE_CLASSES
    assert receipt.labels() == _EXPECTED_RECEIPT_LABELS
    # the promotion class is nowhere on the receipt (not smuggled into another label field).
    assert requested not in receipt.labels().values()

    # the receipt PINS the full id/hash provenance (REQ-116) + the Gate-B revision consumed.
    assert receipt.strategy_id == config.strategy_id
    assert receipt.strategy_revision == "r4b-v0"
    assert receipt.config_hash == config.config_hash()
    assert receipt.observation_hashes == result.observation_hashes
    assert receipt.decision_ids == tuple(d.decision_id for d in result.decisions)
    assert receipt.terminal_state_hash == result.state_hash
    assert receipt.decisions_digest == result.decisions_digest
    assert receipt.gate_b_status == gate_b_status
    assert receipt.gate_b_evidence_revision == "gate-b-rev-1"
    # the linked state-hash chain: decision i's prior hash is decision i-1's next hash.
    chain = receipt.state_hash_chain
    assert chain[-1] == result.state_hash
    assert len(chain) == len(result.decisions) + 1
    for i, decision in enumerate(result.decisions):
        assert decision.prior_state_hash == chain[i]
        assert decision.next_state_hash == chain[i + 1]


def test_historical_decisions_reproduce_under_pinned_config(tmp_path) -> None:
    """A later config revision gets a new hash, but historical decisions reproduce byte-identically (AC-030/RED-31).

    The receipt pins the ORIGINAL ``config_hash``, so a later config revision (a genuinely new
    ``config_hash``) never rewrites history: replaying the SAME historical tape under its
    originally-pinned config reproduces the byte-identical decision stream, observation hashes, terminal
    state hash, and receipt. Determinism is the load-bearing guarantee — the clock is an explicit
    per-observation input, so the pinned config is a total reproduction key.
    """
    tape = load_tape("healthy")
    original = arm_configs(load_base_config_overrides()).guarded

    # Historical run under the originally-pinned config → its provenance receipt.
    historical = replay_arm(tape, original, tmp_path / "historical")
    receipt0 = mint_run_receipt(
        historical, original, gate_b_status="OPEN", gate_b_evidence_revision="gate-b-rev-1"
    )

    # A later config revision: one knob changes → a genuinely NEW config_hash.
    revised = original.model_copy(update={"half_spread": original.half_spread + 0.005})
    assert revised.config_hash() != original.config_hash()

    # Replaying the SAME historical tape under its ORIGINALLY-pinned config reproduces byte-identically —
    # the later revision does not alter history.
    replayed = replay_arm(tape, original, tmp_path / "replayed")
    assert replayed.decisions == historical.decisions
    assert replayed.decisions_digest == historical.decisions_digest
    assert replayed.observation_hashes == historical.observation_hashes
    assert replayed.state_hash == historical.state_hash

    # the receipt reproduces byte-identically under the pinned config, and pins the ORIGINAL hash — never
    # the revised one.
    receipt1 = mint_run_receipt(
        replayed, original, gate_b_status="OPEN", gate_b_evidence_revision="gate-b-rev-1"
    )
    assert receipt1 == receipt0
    assert receipt0.config_hash == original.config_hash()
    assert receipt0.config_hash != revised.config_hash()
    assert receipt1.decision_ids == receipt0.decision_ids
    assert receipt1.state_hash_chain == receipt0.state_hash_chain


def test_no_research_import() -> None:
    """No ``veridex.research`` import anywhere in the harness/receipt or ablation test source (AST scan).

    A static AST scan (not a runtime import graph) of both the TEST-SIDE harness and this test module:
    every ``import`` / ``from ... import`` is walked and asserted NOT to reference the ranked
    ``veridex.research`` lane. The venue-only baseline arm + the honest run receipt would be a lie if the
    harness could reach the ranked research tier, so this pins the boundary at the syntax level — an
    ``importlib``-style dynamic dodge would still have to name the module as a string, which the broader
    :func:`forbidden_import_hits` regex guard (asserted elsewhere) also covers.
    """
    forbidden_root = "veridex.research"
    for source_path in (HARNESS_MODULE_PATH, Path(__file__).resolve()):
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not (
                        alias.name == forbidden_root
                        or alias.name.startswith(forbidden_root + ".")
                    ), f"{source_path.name}: forbidden import {alias.name!r}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not (
                    module == forbidden_root or module.startswith(forbidden_root + ".")
                ), f"{source_path.name}: forbidden import-from {module!r}"


# ============================================================================================
# E7-T5: losing bounded dust = operational success without promotion (AC-035)
# ============================================================================================
# E6 pins the ablation spine, the load-bearing guard, the six-metric evaluation, and the run receipt.
# E7-T5 pins the EXPERIMENTAL_DUST *definition of success* that sits over all of it: a bounded LOSING
# dust session is OPERATIONAL SUCCESS — the machinery worked (deterministic intents produced, honestly
# abstained where the guard required it, stayed within bounds) — EVEN THOUGH it lost. This is the honesty
# of the word "success" under an experimental-dust regime: it proves SAFETY, not alpha, so success is a
# property of the MACHINERY, never of the PnL sign. The losing session is therefore NOT promoted and NOT
# labelled an edge: its evidence stays NOT_PROVEN_EDGE / EXPERIMENTAL_DUST, unpromoted (a losing run that
# self-promoted, or that only "succeeded" when it made money, would be the exact dishonesty AC-035 bars).


def _dust_operational_success(
    *,
    deterministic: bool,
    produced_intents: bool,
    honestly_abstained: bool,
    within_bounds: bool,
    total_markout_bps: float,
) -> bool:
    """Whether a dust session succeeded OPERATIONALLY — the MACHINERY behaving correctly (AC-035).

    A dust session succeeds operationally when it produced deterministic intents, honestly abstained
    where the guard required it, and stayed within bounds — i.e. the machinery worked. This is the
    EXPERIMENTAL_DUST definition of success: it proves SAFETY, not alpha. ``total_markout_bps`` (the
    realized PnL) is accepted so the contract is explicit, but it is DELIBERATELY NOT consulted — success
    is not contingent on the PnL sign (the same structural blindness ``AblationConclusion.infers_benefit``
    uses to record yet never consult the forbidden single metrics). The E7-T5 mutation ties success to
    positive PnL (append ``and total_markout_bps > 0`` to the return), which makes the bounded LOSING
    session wrongly report NOT successful and ``test_losing_dust_operational_success_unpromoted`` go red.
    """
    return deterministic and produced_intents and honestly_abstained and within_bounds


def test_losing_dust_operational_success_unpromoted(tmp_path) -> None:
    """A bounded LOSING dust session is operational success, yet its evidence stays unpromoted (AC-035).

    Three claims, on a REAL replay whose machinery worked:
      * (a) OPERATIONAL SUCCESS despite the loss — the session produced deterministic intents, honestly
        abstained on the extreme-residual tick, and kept exposure bounded; a bounded NON-POSITIVE realized
        markout does not revoke that success;
      * (b) EVIDENCE STAYS UNPROMOTED — the receipt is NOT_PROVEN_EDGE / EXPERIMENTAL_DUST and never
        PROMOTED / EVIDENCE_GATED, even when an explicit promotion is REQUESTED for the losing run;
      * (c) SUCCESS IS NOT CONTINGENT ON THE PnL SIGN — the SAME machinery reports the SAME success verdict
        under a losing, a break-even, and a winning markout.

    Teeth (mutation): tie operational success to positive PnL — append ``and total_markout_bps > 0`` to
    :func:`_dust_operational_success` — and the bounded LOSING session (negative markout) is wrongly marked
    NOT successful, so the ``lost is True`` assertion in (a) goes red. Measuring success by the machinery,
    never the PnL sign, is exactly what keeps a losing-but-correct dust run honestly labelled a success.
    """
    arms = arm_configs(_control_overrides())
    tape = load_tape("markout")
    baseline = replay_arm(tape, arms.baseline, tmp_path / "a")
    guarded = replay_arm(tape, arms.guarded, tmp_path / "b")
    guarded_again = replay_arm(tape, arms.guarded, tmp_path / "b2")
    report = matched_opportunity_report(baseline, guarded)

    # --- the MACHINERY worked (the operational-success inputs), all from a REAL replay -----------
    # deterministic: the same tape + config replayed twice is byte-identical — intents, decisions
    # digest, terminal state hash — and the sealed on-disk tape re-hashes both times.
    deterministic = (
        guarded.decisions == guarded_again.decisions
        and guarded.decisions_digest == guarded_again.decisions_digest
        and guarded.state_hash == guarded_again.state_hash
        and guarded.byte_reproduces
        and guarded_again.byte_reproduces
    )
    assert deterministic

    # produced intents: the session genuinely proposed priced two-sided quotes (not an all-abstain run).
    quote_legs = [
        leg
        for decision in guarded.decisions
        for leg in decision.intent_plan
        if leg.leg_role in ("bid", "ask") and leg.price is not None
    ]
    produced_intents = any(
        decision.kind in ("QUOTE_TWO_SIDED", "QUOTE_ONE_SIDED") and bool(decision.intent_plan)
        for decision in guarded.decisions
    )
    assert produced_intents
    assert quote_legs  # non-vacuous: the run really rested priced legs.

    # honestly abstained where required: the guard fired a REAL fail-closed NO_QUOTE on the
    # extreme-residual tick (feeding the abstention metric) — honest abstention, NOT 'fewer trades'.
    honestly_abstained = report.abstention_count >= 1
    assert honestly_abstained
    assert guarded.decisions[-1].kind == "NO_QUOTE"  # the extreme-residual fail-closed abstention.

    # within bounds: every resting leg is a bounded unit-notional price in (0, 1), so capital at risk can
    # never exceed the resting-leg count — no unbounded exposure — and the report exposes every fill it
    # counts (no favorable subset hidden inside).
    within_bounds = (
        all(0.0 < leg.price < 1.0 for leg in quote_legs)
        and 0.0 < report.capital_at_risk <= len(quote_legs)
        and report.fill_count == len(report.fills)
    )
    assert within_bounds

    # --- the session LOST: a bounded, NON-POSITIVE realized markout -----------------------------
    # The frozen fixtures never post a losing arm — the markout tape is a *winning* session
    # (``total_markout_bps > 0``, asserted next) — so, exactly as E6-T4's adversarial-favorable arm pair
    # constructs its losing metrics, the bounded LOSS here is an explicit construction. It is bounded (a
    # small negative markout), never an unbounded blow-up.
    winning_markout_bps = arm_single_metrics(guarded).total_markout_bps
    assert winning_markout_bps > 0.0  # the real replay actually WON ...
    losing_markout_bps = -120.0  # ... but AC-035 is the claim about a bounded LOSING session.
    assert losing_markout_bps < 0.0

    # (a) OPERATIONAL SUCCESS despite the loss — the machinery worked, so the losing session succeeds.
    lost = _dust_operational_success(
        deterministic=deterministic,
        produced_intents=produced_intents,
        honestly_abstained=honestly_abstained,
        within_bounds=within_bounds,
        total_markout_bps=losing_markout_bps,
    )
    assert lost is True

    # (c) success is NOT contingent on the PnL sign — the SAME machinery under a winning markout, a
    # break-even markout, and the losing markout yields the SAME verdict; the sign is never consulted.
    won = _dust_operational_success(
        deterministic=deterministic,
        produced_intents=produced_intents,
        honestly_abstained=honestly_abstained,
        within_bounds=within_bounds,
        total_markout_bps=winning_markout_bps,
    )
    breakeven = _dust_operational_success(
        deterministic=deterministic,
        produced_intents=produced_intents,
        honestly_abstained=honestly_abstained,
        within_bounds=within_bounds,
        total_markout_bps=0.0,
    )
    assert won is True
    assert breakeven is True
    assert lost == won == breakeven  # identical verdict across losing / break-even / winning PnL.

    # the machinery genuinely GATES the verdict — break any machinery input and success collapses, so the
    # True above is load-bearing (not a constant). PnL sign is the ONE input that never gates it.
    assert (
        _dust_operational_success(
            deterministic=False,
            produced_intents=produced_intents,
            honestly_abstained=honestly_abstained,
            within_bounds=within_bounds,
            total_markout_bps=winning_markout_bps,
        )
        is False
    )
    assert (
        _dust_operational_success(
            deterministic=deterministic,
            produced_intents=produced_intents,
            honestly_abstained=False,
            within_bounds=within_bounds,
            total_markout_bps=winning_markout_bps,
        )
        is False
    )

    # (b) EVIDENCE STAYS UNPROMOTED — losing dust is NOT an edge and is NEVER promoted. Even an explicit
    # request to promote the losing run fails closed: the receipt stays EXPERIMENTAL_DUST / NOT_PROVEN_EDGE
    # and carries NO promotion class (never PROMOTED / EVIDENCE_GATED).
    receipt = mint_run_receipt(
        guarded,
        arms.guarded,
        gate_b_status="OPEN",
        gate_b_evidence_revision="gate-b-rev-1",
        requested_evidence_class="PROMOTED",
    )
    assert isinstance(receipt, RunReceipt)
    assert receipt.evidence_class == "EXPERIMENTAL_DUST"
    assert receipt.edge_label == "NOT_PROVEN_EDGE"
    assert receipt.evidence_class not in PROMOTED_EVIDENCE_CLASSES
    assert not (PROMOTED_EVIDENCE_CLASSES & set(receipt.labels().values()))
    assert receipt.labels() == _EXPECTED_RECEIPT_LABELS
