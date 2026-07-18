"""II-10 — Sharp-Momentum pre-registered ADMISSION GATE (performance-blind, honest-outcome).

A roster contestant must EARN admission on data — never by template. This harness runs
``momentum-sharp`` (:mod:`veridex.strategies.momentum`, consumed UNCHANGED) over a PINNED replay
set and admits it to the leaderboard roster **iff it produces ``>= MIN_SCOREABLE_ACTIONS`` non-WAIT
scoreable actions on each of ``>= MIN_GENUINE_FIXTURES`` GENUINE TxLINE fixtures**. Three honesty
invariants make the verdict trustworthy rather than flattering:

* **Pre-registration.** The fixture set (:data:`PINNED_FIXTURES`) is a frozen constant, pinned
  BEFORE any outcome is read. Which fixtures are evaluated never depends on their results — the pin
  cannot be quietly reshaped to the packs that happen to admit.
* **Performance-blind.** Admission reads ACTION COUNTS only. The single place a CLV is inspected
  (:func:`is_scoreable`) reads validity + numeric-ness and is BLIND to the CLV sign; :func:`evaluate_gate`
  takes counts + a genuine flag and has no CLV input at all. A losing (−CLV) action counts exactly
  like a winning one, so the gate can never be gamed toward (or away from) a favourable PnL.
* **Genuine-or-nothing.** A fixture counts only if its pack reads genuine via
  :func:`veridex.ingest.capture_chain.is_genuine_pack` — a hash-verified ``pack_version>=2`` pack in
  a coherent ``genuine-txline`` state. SYNTHETIC / research-grade (legacy v1) packs may exercise the
  HARNESS but can NEVER admit. Fewer than :data:`MIN_GENUINE_FIXTURES` genuine fixtures ⇒ TEMPLATE-ONLY.

TEMPLATE-ONLY is a fully valid, honest outcome — the harness reports what the data shows and never
forces admission. Non-trust-path module (no LLM SDK, no evidence/law-sealing writes): it consumes the
deterministic law (:func:`veridex.law.recompute.recompute`) purely to decide whether an action is
scoreable, exactly as the orchestrator does.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veridex.ingest.capture_chain import is_genuine_pack
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.law.recompute import PENDING, REPLAY, recompute
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.momentum import SharpMomentumStrategy

#: Repo-root-relative fixture roots. Resolved from this file (``veridex/research/…`` → repo root two
#: levels up) so the harness stays self-contained — it does NOT import the heavy ``scripts.demo_phase2d``
#: entry-point graph. Mirrors that module's own ``fixtures/`` layout and its pinned pack directories.
_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The banked GENUINE demo pack (hash-verified ``genuine-txline`` — see ``scripts.demo_phase2d``).
DEMO_PACK_REAL_DIR = _REPO_ROOT / "scripts" / "fixtures" / "demo_pack_real"
#: The shipped SYNTHETIC illustrative demo pack (visibly non-genuine).
SYNTHETIC_PACK_DIR = _REPO_ROOT / "scripts" / "fixtures" / "demo_pack"

#: Distinct GENUINE fixtures a candidate must clear the action bar on (v2.10.3 finding #2). Held equal
#: to the demo harness's ``SHARP_MOMENTUM_MIN_FIXTURES`` (both 2) so the gate and demo agree on "how
#: many genuine" without importing that module's entry-point graph.
MIN_GENUINE_FIXTURES = 2
#: Non-WAIT scoreable actions a genuine fixture must yield to qualify (the brief's ">= 3").
MIN_SCOREABLE_ACTIONS = 3


@dataclass(frozen=True)
class FixtureCandidate:
    """One pinned ``(pack_dir, fixture_id)`` candidate — a fixture is classified by its PACK."""

    pack_dir: Path
    fixture_id: int


#: The GENUINE fixture ids banked in :data:`DEMO_PACK_REAL_DIR` (FIFA WC 2026 quarter-finals). Written
#: as explicit literals so the pin is a visible, reviewable constant — pinned BEFORE any outcome is read.
_GENUINE_FIXTURE_IDS: tuple[int, ...] = (18209181, 18213979, 18218149, 18222446)
#: The shipped SYNTHETIC demo-pack fixture, pinned as a NON-GENUINE control: the harness classifies it
#: non-genuine and excludes it from admission no matter how many actions it produces.
_SYNTHETIC_FIXTURE_ID = 17588404

#: PRE-REGISTERED pinned replay set — frozen BEFORE outcomes are read. The genuine WC fixtures plus a
#: synthetic control that proves the classifier gates admission on provenance, not on action volume.
PINNED_FIXTURES: tuple[FixtureCandidate, ...] = (
    *(FixtureCandidate(DEMO_PACK_REAL_DIR, fid) for fid in _GENUINE_FIXTURE_IDS),
    FixtureCandidate(SYNTHETIC_PACK_DIR, _SYNTHETIC_FIXTURE_ID),
)


@dataclass(frozen=True)
class FixtureGateResult:
    """Per-fixture gate row: whether the pack is genuine and how many actions it scored."""

    pack_dir: Path
    fixture_id: int
    genuine: bool
    scoreable_actions: int
    total_non_wait: int


@dataclass(frozen=True)
class GateVerdict:
    """The binary admission verdict plus the honest per-fixture evidence behind it."""

    admitted: bool
    verdict: str  # "ADMITTED" | "TEMPLATE-ONLY"
    genuine_fixture_count: int
    qualifying_fixture_count: int
    reason: str
    results: tuple[FixtureGateResult, ...]


def pinned_fixtures() -> tuple[FixtureCandidate, ...]:
    """Return the pre-registered pin verbatim (surfaced as a function so callers never re-select it)."""
    return PINNED_FIXTURES


def is_scoreable(recompute_result: dict[str, Any]) -> bool:
    """Whether a non-WAIT action is SCOREABLE — the ONLY place a CLV is ever inspected.

    Reads validity + numeric-ness ONLY; the SIGN of ``clv_bps`` is NEVER consulted (performance-blind).
    An action is scoreable exactly when the law returns ``valid`` AND a NUMERIC ``clv_bps`` (an ``int``)
    — i.e. it entered a usable market that reached a closing horizon. A :data:`~veridex.law.recompute.PENDING`
    clv (WAIT abstention / awaiting-close) or any ``valid=False`` verdict is not scoreable.
    """
    if not recompute_result.get("valid"):
        return False
    clv = recompute_result.get("clv_bps")
    # `bool` is an int subclass; exclude it defensively so a stray True/False can't read as a CLV.
    return clv != PENDING and isinstance(clv, int) and not isinstance(clv, bool)


def _closing_by_market(marketstates: Sequence[MarketState]) -> dict[str, MarketState]:
    """Map each ``market_key`` to its closing-horizon snapshot (the last tick carrying it).

    Mirrors :func:`veridex.runtime.orchestrator._closing_snapshots` exactly so a replayed action is
    scored against the SAME closing line the live arena would use — later ticks overwrite earlier ones.
    """
    closing: dict[str, MarketState] = {}
    for state in marketstates:
        for market_key in state.markets:
            closing[market_key] = state
    return closing


def count_scoreable_actions(pack_dir: Path, fixture_id: int) -> tuple[int, int]:
    """Replay ``momentum-sharp`` over one fixture; return ``(scoreable_count, total_non_wait)``.

    Deterministic and causal: a fresh :class:`~veridex.strategies.momentum.SharpMomentumStrategy` sees
    ticks in order (data ``<= t`` only), and each non-WAIT action is scored against the shared closing
    horizon via the deterministic law — counting ONLY whether it is scoreable, never its CLV sign.
    """
    marketstates = load_pack_marketstates(pack_dir, fixture_id)
    closing = _closing_by_market(marketstates)
    strategy = SharpMomentumStrategy()

    scoreable = 0
    total_non_wait = 0
    for state in marketstates:
        action: AgentAction = strategy.decide(state)
        if action.type == SportsActionType.WAIT:
            continue
        total_non_wait += 1
        market_key = (action.params or {}).get("market_key")
        closing_state = closing.get(market_key) if market_key else None
        result = recompute(state, action, closing=closing_state, source_mode=REPLAY)
        if is_scoreable(result):
            scoreable += 1
    return scoreable, total_non_wait


def evaluate_gate(results: Sequence[FixtureGateResult]) -> GateVerdict:
    """Apply the binary admission rule — a PURE function of action COUNTS + the genuine flag.

    ADMITTED iff ``>= MIN_GENUINE_FIXTURES`` GENUINE fixtures each cleared ``>= MIN_SCOREABLE_ACTIONS``
    scoreable actions. Non-genuine fixtures are excluded outright (synthetic-never-admits), so a huge
    action count on a synthetic/research-grade pack can never move the verdict. No CLV enters here.
    """
    genuine = [r for r in results if r.genuine]
    qualifying = [r for r in genuine if r.scoreable_actions >= MIN_SCOREABLE_ACTIONS]
    admitted = len(qualifying) >= MIN_GENUINE_FIXTURES

    if admitted:
        reason = (
            f"ADMITTED — {len(qualifying)} genuine fixtures each >= {MIN_SCOREABLE_ACTIONS} scoreable "
            f"actions (>= {MIN_GENUINE_FIXTURES} required)"
        )
    elif len(genuine) < MIN_GENUINE_FIXTURES:
        reason = (
            f"TEMPLATE-ONLY — only {len(genuine)} genuine fixture(s) found "
            f"(< {MIN_GENUINE_FIXTURES}); synthetic/research-grade packs cannot admit"
        )
    else:
        reason = (
            f"TEMPLATE-ONLY — {len(qualifying)} of {len(genuine)} genuine fixtures cleared "
            f">= {MIN_SCOREABLE_ACTIONS} scoreable actions (< {MIN_GENUINE_FIXTURES} required)"
        )

    return GateVerdict(
        admitted=admitted,
        verdict="ADMITTED" if admitted else "TEMPLATE-ONLY",
        genuine_fixture_count=len(genuine),
        qualifying_fixture_count=len(qualifying),
        reason=reason,
        results=tuple(results),
    )


def run_gate(
    candidates: Sequence[FixtureCandidate] | None = None,
    *,
    count_fn: Callable[[Path, int], tuple[int, int]] = count_scoreable_actions,
    genuine_fn: Callable[[Path], bool] = is_genuine_pack,
) -> GateVerdict:
    """Classify → replay-count → apply the binary rule over the PINNED set (or injected candidates).

    ``count_fn``/``genuine_fn`` are test seams (default to the real replay + real classifier). The
    candidate set is the pre-registered pin when ``candidates`` is ``None`` — it is consumed verbatim
    and in order, so the evaluated fixtures never depend on their outcomes (pre-registration).
    """
    pinned = pinned_fixtures() if candidates is None else tuple(candidates)
    results: list[FixtureGateResult] = []
    for candidate in pinned:
        genuine = genuine_fn(candidate.pack_dir)
        scoreable, total_non_wait = count_fn(candidate.pack_dir, candidate.fixture_id)
        results.append(
            FixtureGateResult(
                pack_dir=candidate.pack_dir,
                fixture_id=candidate.fixture_id,
                genuine=genuine,
                scoreable_actions=scoreable,
                total_non_wait=total_non_wait,
            )
        )
    return evaluate_gate(results)
