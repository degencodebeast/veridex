"""E6 — R2 ex-ante fill-assumption contract tests.

R2 is a DECLARED MODEL OVERLAY, never a fill/PnL/edge claim. The fill rule uses
only ex-ante fields available AT QUOTE TIME; tape-reactive triggers are rejected
at construction. Every new pin field must flow into ``config_hash()``.
"""

import pytest
from pydantic import ValidationError

from veridex.maker.r2_bracket import FillAssumptionConfig


def _cfg(**kw):
    base = dict(
        fill_model_id="m1",
        latency_ms=250,
        cross_rule="mid",
        partial_fill_policy="none",
        fill_probability_rule="static_fill_prob",
        rule_params={"p": 0.2},
        draw_mode="DETERMINISTIC_EXPECTED",
        seed=None,
        n_paths=None,
        ex_ante_fields=["quote_price", "quoted_half_spread"],
    )
    base.update(kw)
    return FillAssumptionConfig(**base)


def test_fill_assumption_rule_params_move_config_hash():
    base = _cfg().config_hash()
    # changing rule_params moves the hash
    assert _cfg(rule_params={"p": 0.9}).config_hash() != base
    # changing draw_mode moves the hash
    assert _cfg(draw_mode="SEEDED_STOCHASTIC", seed=7, n_paths=100).config_hash() != base
    # changing seed moves the hash
    seeded = _cfg(draw_mode="SEEDED_STOCHASTIC", seed=1, n_paths=10)
    assert seeded.config_hash() != _cfg(
        draw_mode="SEEDED_STOCHASTIC", seed=2, n_paths=10
    ).config_hash()
    # changing fill_probability_rule moves the hash
    assert _cfg(fill_probability_rule="static_touch_prob").config_hash() != base
    # changing ex_ante_fields moves the hash
    assert _cfg(ex_ante_fields=["quote_price"]).config_hash() != base
    # changing n_paths moves the hash
    assert _cfg(
        draw_mode="SEEDED_STOCHASTIC", seed=1, n_paths=10
    ).config_hash() != _cfg(
        draw_mode="SEEDED_STOCHASTIC", seed=1, n_paths=20
    ).config_hash()


def test_queue_modeled_true_still_rejected():
    with pytest.raises(ValidationError):
        _cfg(queue_modeled=True)


def test_forbidden_tape_reactive_trigger_rejected():
    # a forbidden trigger named as the fill rule is rejected
    with pytest.raises(ValidationError):
        FillAssumptionConfig(
            fill_model_id="m",
            latency_ms=1,
            cross_rule="mid",
            partial_fill_policy="none",
            fill_probability_rule="trade_crossed",
            rule_params={},
            draw_mode="DETERMINISTIC_EXPECTED",
            seed=None,
            n_paths=None,
            ex_ante_fields=["quote_price"],
        )
    # a forbidden trigger named in ex_ante_fields is also rejected
    with pytest.raises(ValidationError):
        FillAssumptionConfig(
            fill_model_id="m",
            latency_ms=1,
            cross_rule="mid",
            partial_fill_policy="none",
            fill_probability_rule="static_touch_prob",
            rule_params={"p": 0.1},
            draw_mode="DETERMINISTIC_EXPECTED",
            seed=None,
            n_paths=None,
            ex_ante_fields=["fv_moved"],
        )
    # every forbidden trigger is rejected as a fill rule
    for trigger in ["trade_crossed", "price_touched", "mid_crossed", "fv_moved", "post_hoc_fill"]:
        with pytest.raises(ValidationError):
            _cfg(fill_probability_rule=trigger)


def test_forbidden_trigger_rejected_case_insensitive():
    # M1/CON-107: an UPPER/mixed-case forbidden trigger must NOT slip through the
    # substring guard (case-insensitive matching).
    with pytest.raises(ValidationError):
        _cfg(fill_probability_rule="TRADE_CROSSED")


def test_forbidden_trigger_rejected_in_decorated_ex_ante_field():
    # M1/CON-107: a decorated field embedding a forbidden trigger substring
    # (e.g. "trade_crossed_signal") must be rejected via substring matching.
    with pytest.raises(ValidationError):
        _cfg(ex_ante_fields=["trade_crossed_signal"])


def test_seeded_stochastic_requires_seed_and_n_paths():
    # M2/CON-108: SEEDED_STOCHASTIC claims a pinned run -> seed and n_paths must
    # be pinned (seed not None, n_paths a positive int), else config_hash lies.
    with pytest.raises(ValidationError):
        _cfg(draw_mode="SEEDED_STOCHASTIC", seed=None, n_paths=100)
    with pytest.raises(ValidationError):
        _cfg(draw_mode="SEEDED_STOCHASTIC", seed=7, n_paths=None)
    with pytest.raises(ValidationError):
        _cfg(draw_mode="SEEDED_STOCHASTIC", seed=7, n_paths=0)
    with pytest.raises(ValidationError):
        _cfg(draw_mode="SEEDED_STOCHASTIC", seed=7, n_paths=-5)
    # a fully-pinned SEEDED_STOCHASTIC config is accepted
    _cfg(draw_mode="SEEDED_STOCHASTIC", seed=7, n_paths=100)
    # DETERMINISTIC_EXPECTED is unaffected by the seed/n_paths requirement
    _cfg(draw_mode="DETERMINISTIC_EXPECTED", seed=None, n_paths=None)


def test_draw_mode_rejects_unknown_value():
    # m1: a typo'd draw_mode must be rejected, not silently treated deterministic.
    with pytest.raises(ValidationError):
        _cfg(draw_mode="SEEDED")


def test_non_allowlisted_fill_rule_rejected():
    # CON-106 (Gate-#3 Major 1): ex-ante honesty is proven by an ALLOWLIST, not a
    # denylist. A rule that references a later observation but avoids the 5
    # forbidden tokens (e.g. "uses_future_score_after_quote") slips the denylist,
    # so the allowlist must reject any fill rule it cannot actually compute.
    with pytest.raises(ValidationError):
        _cfg(fill_probability_rule="uses_future_score_after_quote")


def test_non_allowlisted_ex_ante_field_rejected():
    # CON-106 (Gate-#3 Major 1): a declared ex-ante field that names a later
    # observation ("future_score_after_quote") avoids the 5 forbidden tokens but
    # is NOT a quote-time input -> the allowlist must reject it.
    with pytest.raises(ValidationError):
        _cfg(ex_ante_fields=["future_score_after_quote"])


def test_split_or_renamed_realized_trigger_rejected():
    # CON-106 (Gate-#3 Major 1): a future-/realized-referencing name that avoids
    # every forbidden token ("realized_edge_next_bar") must still be rejected —
    # positive ex-ante proof (allowlist), not open-ended denylist.
    with pytest.raises(ValidationError):
        _cfg(ex_ante_fields=["realized_edge_next_bar"])
