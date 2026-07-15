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

import time

from tests.mm_strategy_ablation_harness import (
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
