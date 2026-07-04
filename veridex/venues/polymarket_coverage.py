"""Polymarket 1X2 feasibility probe + coverage gate (C-1, REQ-001, CON-001).

Pure data-shape + threshold module (no network, no imports beyond the stdlib + pydantic) — the
HARD feasibility gate that decides whether the C/P1 rung-2 lane is viable at all. The operator
probe shell (:mod:`scripts.txline_live.cp1_probe`) fetches Polymarket ``prices-history`` per 1X2
side and hands the counted, freshness-bucketed inputs here; this module never touches a venue.

The gate (CON-001, concrete thresholds):

* a **side is covered** iff ``pre_kickoff_quote_count >= min_pre_kickoff`` (default 5);
* a **fixture is headline-eligible** iff ALL THREE 1X2 sides (home/away/draw) are covered;
* any uncovered side carries a NAMED :attr:`VenueSideCoverage.partial_side_missing` reason and
  demotes the fixture to diagnostic-only — a partial-coverage fixture is NEVER promoted to the
  headline estimated-edge universe (it is shown, not silently mixed).

:func:`coverage_content_hash` gives the coverage artifact a deterministic content hash so a
predeclared run (Run-002-VvV) can pin the exact probe-passing universe BEFORE any estimated-edge
number exists — thresholds are never tuned after seeing edge outcomes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel

# The three 1X2 match-winner sides, in a fixed order so coverage output is deterministic.
SIDES: tuple[str, ...] = ("home", "away", "draw")


@dataclass(frozen=True)
class SideInput:
    """Per-side probe inputs feeding the coverage gate (pure; the probe shell fills these).

    ``pre_kickoff_quote_count`` is the only load-bearing field for the covered/headline rule
    (CON-001); the rest are diagnostic context carried through onto :class:`VenueSideCoverage`.
    ``token_resolved=False`` marks a side whose Polymarket token could not be resolved (e.g. the
    market lacks a draw token) — an uncovered side either way, but with a distinct named reason.
    """

    pre_kickoff_quote_count: int
    quote_count: int | None = None  # total observed; defaults to the pre-kickoff count
    freshness_bucket_counts: dict[str, int] = field(default_factory=dict)
    first_quote_ts: int | None = None
    last_quote_ts: int | None = None
    token_resolved: bool = True


class VenueSideCoverage(BaseModel):
    """Coverage summary for one 1X2 side of one fixture (REQ-001, spec §4)."""

    side: str  # home | away | draw
    token_resolved: bool
    quote_count: int
    pre_kickoff_quote_count: int
    freshness_bucket_counts: dict[str, int]  # "<=2m" / "<=5m" / "<=15m" -> count
    first_quote_ts: int | None
    last_quote_ts: int | None
    covered: bool  # pre_kickoff_quote_count >= min_pre_kickoff
    partial_side_missing: str | None  # named reason when uncovered, else None


class VenueCoverage(BaseModel):
    """Per-fixture coverage: the three side summaries + the all-three-sides headline verdict."""

    fixture_id: int
    sides: list[VenueSideCoverage]
    headline_eligible: bool  # all three sides covered (CON-001)
    reason: str | None  # None when eligible, else a compact named summary of what is missing


def _normalize_side_input(value: int | SideInput | Mapping[str, object]) -> SideInput:
    """Accept an int count, a :class:`SideInput`, or a plain mapping and normalize to SideInput."""
    if isinstance(value, SideInput):
        return value
    if isinstance(value, int):
        return SideInput(pre_kickoff_quote_count=value)
    return SideInput(**value)  # type: ignore[arg-type]


def _side_missing_reason(*, token_resolved: bool, pre_kickoff: int, minimum: int) -> str:
    """Name WHY a side is uncovered (never a silent False)."""
    if not token_resolved:
        return "token_unresolved"
    if pre_kickoff <= 0:
        return "no_pre_kickoff_quotes"
    return f"thin_pre_kickoff_quotes({pre_kickoff}<{minimum})"


def evaluate_venue_coverage(
    fixture_id: int,
    side_inputs: Mapping[str, int | SideInput | Mapping[str, object]],
    *,
    kickoff_ts: int,
    min_pre_kickoff: int = 5,
) -> VenueCoverage:
    """Apply the CON-001 coverage gate to one fixture's per-side probe inputs.

    Args:
        fixture_id: The fixture being probed.
        side_inputs: Per-side inputs keyed by ``"home"``/``"away"``/``"draw"``. Each value is
            either a bare pre-kickoff quote count (int) or a rich :class:`SideInput` (count +
            freshness buckets + first/last ts + token-resolved flag). A side absent from the map
            is treated as an unresolved, uncovered side (fail closed, never assumed covered).
        kickoff_ts: The pre-kickoff boundary the caller (the probe shell) used UPSTREAM to derive
            ``pre_kickoff_quote_count`` and the freshness buckets. Accepted for signature stability
            and documentation only — the pure gate operates on the already-counted ``SideInput``s,
            so ``kickoff_ts`` is NOT referenced in the body and NOT persisted into
            :class:`VenueCoverage` or its content hash. (Persist it here only if a future run needs
            it pinned; today it is not.)
        min_pre_kickoff: The covered threshold (CON-001 default 5).

    Returns:
        A :class:`VenueCoverage` whose ``headline_eligible`` is True iff all three 1X2 sides are
        covered; uncovered sides carry a named ``partial_side_missing`` reason.
    """
    sides: list[VenueSideCoverage] = []
    for side in SIDES:
        raw = side_inputs.get(side)
        si = _normalize_side_input(raw) if raw is not None else SideInput(pre_kickoff_quote_count=0, token_resolved=False)
        covered = si.token_resolved and si.pre_kickoff_quote_count >= min_pre_kickoff
        reason = (
            None
            if covered
            else _side_missing_reason(
                token_resolved=si.token_resolved,
                pre_kickoff=si.pre_kickoff_quote_count,
                minimum=min_pre_kickoff,
            )
        )
        sides.append(
            VenueSideCoverage(
                side=side,
                token_resolved=si.token_resolved,
                quote_count=si.quote_count if si.quote_count is not None else si.pre_kickoff_quote_count,
                pre_kickoff_quote_count=si.pre_kickoff_quote_count,
                freshness_bucket_counts=dict(si.freshness_bucket_counts),
                first_quote_ts=si.first_quote_ts,
                last_quote_ts=si.last_quote_ts,
                covered=covered,
                partial_side_missing=reason,
            )
        )

    headline_eligible = all(s.covered for s in sides)
    uncovered = [f"{s.side}:{s.partial_side_missing}" for s in sides if not s.covered]
    reason = None if headline_eligible else "uncovered=" + ",".join(uncovered)
    return VenueCoverage(
        fixture_id=fixture_id,
        sides=sides,
        headline_eligible=headline_eligible,
        reason=reason,
    )


def coverage_content_hash(cov: VenueCoverage) -> str:
    """Deterministic sha256 over the canonical (sorted-keys, compact) coverage artifact.

    Lets a predeclared run pin the exact probe-passing universe before any estimated-edge number
    exists (CON-001) — the same coverage content always hashes the same, and any change to a
    count / bucket / covered flag / reason changes the hash.
    """
    payload = json.dumps(cov.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
