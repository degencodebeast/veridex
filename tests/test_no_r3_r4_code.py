"""Revert-proof guard: no R3/R4 execution code in the scored maker lane.

MM-R3 (queue-position fill SIMULATION) and MM-R4 (real OWN-FILL reconciliation)
are DECLARED but OUT OF SCOPE — hard-gated behind a future live-data recorder
(the venue is mids-only: no L2/depth, no cancels, no own-order lifecycle). This
guard makes that boundary revert-proof at two seams:

1. No R3/R4 execution symbol may be DEFINED anywhere in ``veridex.maker.*``. The
   check is AST-based (class/function/assignment *definitions*, not substrings),
   so a docstring that merely *mentions* R3/R4 does not false-trip.
2. ``assign_rung`` must never return R3/R4 — even with every presence flag set,
   the MM-2 data-feasibility gate caps at MM-R1.5.

See ``docs/maker/r3-r4-recorder-checklist.md`` for the recorder a future
operator-gated step must satisfy before any R3/R4 claim is admissible.
"""

import ast
import importlib
import inspect
import pkgutil

import veridex.maker as maker_pkg
from veridex.maker.contracts import MakerRungLabel
from veridex.maker.rung_gate import DataPresence, assign_rung

# R3/R4 execution symbols that must NOT be DEFINED anywhere in the scored lane.
FORBIDDEN_DEFS = {
    "SimulatedFill",
    "simulate_fill",
    "BookSnapshot",
    "BookDelta",
    "queue_position",
    "fill_simulation",
    "simulate_queue",
    "OrderLifecycleEvent",
    "RealFillReconciliation",
}


def _defined_names(modname: str) -> set[str]:
    """Top-level+nested class/function/assignment names DEFINED in a module."""
    tree = ast.parse(inspect.getsource(importlib.import_module(modname)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_no_r3_r4_execution_code_defined_in_maker_lane() -> None:
    for mod in pkgutil.iter_modules(maker_pkg.__path__, prefix="veridex.maker."):
        defined = _defined_names(mod.name)
        clash = defined & FORBIDDEN_DEFS
        assert not clash, (
            f"{mod.name} defines forbidden R3/R4 execution symbol(s): {clash}"
        )


def test_assign_rung_never_returns_r3_or_r4() -> None:
    # Even with EVERY presence flag set True, the gate caps at MM-R1.5 — R3/R4
    # are future-only (depth/cancels/own-fills are accepted but ignored here).
    presence = DataPresence(
        has_mids=True,
        has_trades=True,
        has_fill_assumption=True,
        has_l2_depth=True,
        has_cancels=True,
        has_own_fills=True,
    )
    assert assign_rung(presence) not in (MakerRungLabel.R3, MakerRungLabel.R4)
