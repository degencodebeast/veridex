"""II-2 — the DETERMINISTIC synthetic inventory stub (plan decision A5).

The replay/dry-run maker composition root has NO reconciled venue inventory: a dry-run run places no
real orders, so there is no honest net-position/open-order truth to reason over. Rather than fabricate
one silently, this module produces a DETERMINISTIC, FLAT :class:`InventoryProjection` and — critically
— stamps every OPS event it feeds with an explicit ``SYNTHETIC`` label. The honesty guarantee (A5):
this projection is a STUB, never reconciled truth, and it can never masquerade as a real inventory in
the Agent-Ops telemetry.

Two load-bearing properties:

  * **Deterministic + flat.** :func:`synthetic_inventory` returns the SAME flat projection
    (``net_position == 0.0``, no resting orders, ``fresh``) for a given ``as_of_ts`` — a pure function
    of its input, so the composition loop stays byte-deterministic (II-2 RED#2).
  * **Synthetic-labeled in EVERY OPS event.** :func:`synthetic_inventory_event` is the ONLY way this
    stub reaches the OPS channel, and it ALWAYS sets ``synthetic=True`` +
    ``inventory_source=SYNTHETIC`` in the payload. There is no un-labeled emission path.

This module is deliberately framework-free (AC-24): it imports NO ``agno`` / AgentOS symbol.
"""

from __future__ import annotations

from veridex.mm_strategy.contracts import InventoryProjection
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType, runtime_event

__all__ = [
    "SYNTHETIC_INVENTORY_LABEL",
    "synthetic_inventory",
    "synthetic_inventory_event",
]

#: The closed-vocab honesty label stamped on EVERY OPS event this stub feeds. A downstream consumer
#: (cockpit drawer, audit) reads this to know the inventory is a replay/dry-run STUB, never reconciled
#: venue truth (A5). It is a module constant — never a caller/agent parameter.
SYNTHETIC_INVENTORY_LABEL = "SYNTHETIC"


def synthetic_inventory(*, as_of_ts: int) -> InventoryProjection:
    """Return the DETERMINISTIC flat synthetic inventory projection (A5).

    A pure function of ``as_of_ts``: a FLAT book (``net_position == 0.0``, no resting orders) marked
    ``fresh`` so a replay/dry-run decision is never data-degraded by a stale-projection gate. This is a
    STUB — it is NOT reconciled venue truth and must always be surfaced as :data:`SYNTHETIC_INVENTORY_LABEL`
    in telemetry (see :func:`synthetic_inventory_event`).

    Args:
        as_of_ts: The observation clock the flat projection is stamped as-of (keeps it non-future-dated
            relative to the decision it accompanies).

    Returns:
        A flat, fresh :class:`InventoryProjection` — deterministic for a given ``as_of_ts``.
    """
    return InventoryProjection(
        net_position=0.0,
        resting=(),
        projection_as_of_ts=as_of_ts,
        fresh=True,
    )


def synthetic_inventory_event(
    inventory: InventoryProjection,
    *,
    agent_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> RuntimeEvent:
    """Project the synthetic inventory into ONE OPS ``RuntimeEvent``, ALWAYS labeled synthetic (A5).

    The SOLE path this stub reaches the OPS channel. The payload ALWAYS carries ``synthetic=True`` and
    ``inventory_source=SYNTHETIC`` alongside the flat projection's scalar fields, so no un-labeled
    synthetic inventory can ever appear in Agent-Ops telemetry. The event is a structurally
    non-evidence ``RuntimeEvent`` (no ``sequence_no`` / ``payload_hash``), so it can never be sealed,
    scored, or ranked — it is read-only OPS telemetry.

    Args:
        inventory: The synthetic projection to surface (built by :func:`synthetic_inventory`).
        agent_id: Telemetry identity stamped on the event (NOT a policy input).
        run_id: Optional OPS correlation id.
        session_id: Optional runtime session id.

    Returns:
        A synthetic-labeled ``ACTION_EMITTED`` OPS ``RuntimeEvent``.
    """
    return runtime_event(
        RuntimeEventType.TOOL_CALL,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        telemetry="synthetic_inventory_projection",
        inventory_source=SYNTHETIC_INVENTORY_LABEL,
        synthetic=True,
        net_position=inventory.net_position,
        resting_count=len(inventory.resting),
        projection_as_of_ts=inventory.projection_as_of_ts,
        fresh=inventory.fresh,
    )
