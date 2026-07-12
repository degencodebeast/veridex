"""E2-T6 tests for mechanical fixed-fraction dust sizing (GUD-001, AC-024; Codex-M5).

Trust boundary proven here: R4-A's executable dust size is a DETERMINISTIC, mechanical
fixed-fraction of the pinned ``wallet_equity_at_decision`` — never raw Kelly, never a
discretionary/confidence term, and never an agent-supplied ``size``. Both the arithmetic
and the STRUCTURAL signature guard matter: the arithmetic test proves the value is the
fixed fraction (capped), and the signature test proves — structurally, not just
arithmetically — that no agent-facing input (``confidence``/``size``/``requested_size``)
can even be passed in to move it. ``fixed_fraction`` + ``wallet_equity_at_decision`` are
pinned manifest/session inputs (E1-T2), not agent-supplied.
"""

import inspect

from veridex.dust_execution.sizing import resolve_dust_size


def test_size_is_mechanical_fixed_fraction_not_kelly_or_confidence():
    s = resolve_dust_size(fixed_fraction=0.001, wallet_equity_at_decision=100.0, max_notional=1.0, max_per_order=1.0)
    assert s == 0.10                                   # 0.001 * 100 = 0.10, deterministic
    # agent confidence / a larger requested size CANNOT increase the executable size:
    s_hi = resolve_dust_size(fixed_fraction=0.001, wallet_equity_at_decision=100.0, max_notional=1.0,
                             max_per_order=1.0)          # no confidence/size param exists to pass
    assert s_hi == s


def test_size_capped_by_max_notional_and_per_order():
    assert resolve_dust_size(fixed_fraction=0.10, wallet_equity_at_decision=100.0, max_notional=1.0, max_per_order=0.5) == 0.5


def test_resolve_dust_size_signature_forbids_agent_inputs():
    # STRUCTURAL guard (Codex-M4/Fable-m3): a helper arithmetic test can't catch an
    # added-but-unused agent param. Assert the parameter NAMES are EXACTLY the 4 pinned
    # inputs — no confidence/size/requested_size — so "an agent can't change the size" is
    # STRUCTURALLY true, not merely arithmetically true.
    params = inspect.signature(resolve_dust_size).parameters
    assert set(params) == {"fixed_fraction", "wallet_equity_at_decision", "max_notional", "max_per_order"}
    # keyword-only, so a positional agent value cannot slip in either.
    for name, p in params.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
