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

import json
import time

from tests.mm_strategy_ablation_harness import (
    FIXTURES_DIR,
    HARNESS_MODULE_PATH,
    arm_configs,
    config_field_diff,
    forbidden_import_hits,
    load_base_config_overrides,
    load_tape,
    neutralize_guard,
    replay_arm,
)

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


def _decision_signature(
    decision,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str | None, float | None, bool], ...]]:
    """The SUBSTANTIVE decision: kind + closed reason codes + priced legs (kind/side/price/post_only).

    EXCLUDES the per-arm identity stamp (``config_hash`` / ``decision_id`` / the four causal hashes /
    per-leg ``client_order_id``), which differs between arms purely because ``guard_enabled`` differs.
    An honest guard A/B contrast is over exactly this substance — the behaviour the guard changes when
    everything else is held equal.
    """
    legs = tuple(
        (leg.kind, leg.leg_role, leg.price, leg.post_only) for leg in decision.intent_plan
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

        base_sig = [_decision_signature(d) for d in baseline.decisions]
        guard_sig = [_decision_signature(d) for d in guarded.decisions]

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

    base_sig = [_decision_signature(d) for d in baseline.decisions]
    guard_sig = [_decision_signature(d) for d in guarded.decisions]
    # NO trigger ⇒ the guarded and baseline decision streams are substance-IDENTICAL.
    assert base_sig == guard_sig

    # non-vacuity: the guarded arm was genuinely LIVE — it saw a fresh FV and actually quoted two-sided
    # on the row-H tick (a warmup-only tape would be a trivial 'both abstain' match, proving nothing).
    assert any(o.guard_fv is not None for o in guarded.observations)
    assert any(d.kind == "QUOTE_TWO_SIDED" for d in guarded.decisions)
    # ... and the arms are NOT byte-identical objects: their identity stamps (config_hash / decision_id)
    # differ, so the match is over decision SUBSTANCE — exactly what an honest A/B holds equal.
    assert guarded.decisions_digest != baseline.decisions_digest


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
