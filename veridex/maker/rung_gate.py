"""MM-2 data-feasibility rung gate.

Assigns a `MakerRungLabel` based purely on which market-data feeds are
present, never on the rung an agent claims to target. This keeps rung
assignment an objective, evidence-based gate rather than a self-report.
"""

from __future__ import annotations

from pydantic import BaseModel

from veridex.maker.contracts import MakerRungLabel


class DataPresence(BaseModel):
    has_mids: bool
    has_trades: bool
    has_fill_assumption: bool
    has_l2_depth: bool = False
    has_cancels: bool = False
    has_own_fills: bool = False


def assign_rung(presence: DataPresence) -> MakerRungLabel | None:
    """Assign a maker rung from data presence alone.

    Returns None (INCONCLUSIVE) when mids are absent. `has_fill_assumption`
    is an R2 overlay flag, not a rung upgrade. `has_l2_depth`, `has_cancels`,
    and `has_own_fills` are accepted but deliberately IGNORED here: R3/R4
    are out of scope for this lane, so the gate must never emit them.
    """
    if not presence.has_mids:
        return None
    return MakerRungLabel("MM-R1.5") if presence.has_trades else MakerRungLabel("MM-R1")
