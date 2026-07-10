"""Two-lane revert-proof guard: no R3/R4 execution code in the SCORED maker lane.

MM-R3 (queue-position fill SIMULATION) and MM-R4 (real OWN-FILL reconciliation)
are DECLARED but OUT OF SCOPE for the scored maker/directional lanes. The MM-R3
live-recorder (``veridex.live_recorder.*``) is a SEPARATE, operator-gated lane that
IS PERMITTED to define/observe those execution symbols (it records COUNTERFACTUAL
executability, never a fill). This guard makes the boundary revert-proof at three
seams:

1. No R3/R4 execution symbol may be DEFINED anywhere in ``veridex.maker.*`` — the
   scan is RECURSIVE (``pkgutil.walk_packages``) so a symbol hidden in a future
   ``veridex.maker.<sub>`` subpackage is still caught. The check is AST-based
   (class/function/assignment *definitions*, not substrings), so a docstring that
   merely *mentions* R3/R4 does not false-trip.
2. The recorder lane ``veridex.live_recorder.*`` is PERMITTED to define those
   symbols: it is never in the maker forbid-scan's scope.
3. The maker/directional lane must not IMPORT the recorder lane
   (``test_maker_and_scoring_do_not_import_live_recorder``), and ``assign_rung``
   must never return R3/R4 — even with every presence flag set, the MM-2
   data-feasibility gate caps at MM-R1.5.

See ``docs/maker/r3-r4-recorder-checklist.md`` for the recorder a future
operator-gated step must satisfy before any R3/R4 claim is admissible.
"""

import ast
import importlib
import inspect
import pkgutil

import veridex.live_recorder as live_recorder_pkg
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


def _walk_modnames(pkg: object, prefix: str) -> list[str]:
    """RECURSIVELY enumerate every module name under a package (subpackages included)."""
    return [info.name for info in pkgutil.walk_packages(pkg.__path__, prefix=prefix)]


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


def _forbidden_in_package(pkg: object, prefix: str) -> dict[str, set[str]]:
    """``{modname: clash}`` for every module (RECURSIVE) DEFINING a forbidden symbol."""
    violations: dict[str, set[str]] = {}
    for modname in _walk_modnames(pkg, prefix):
        clash = _defined_names(modname) & FORBIDDEN_DEFS
        if clash:
            violations[modname] = clash
    return violations


def test_maker_still_forbids_r3r4_symbols() -> None:
    # Lane 1 (FORBID): no R3/R4 execution symbol is DEFINED anywhere in veridex.maker.*
    # — recursively, so a symbol buried in a subpackage cannot slip past the guard.
    violations = _forbidden_in_package(maker_pkg, "veridex.maker.")
    assert not violations, (
        f"scored maker lane defines forbidden R3/R4 execution symbol(s): {violations}"
    )


def test_live_recorder_may_define_r3r4_symbols() -> None:
    # Lane 2 (PERMIT): the recorder lane is a SEPARATE, operator-gated lane that MAY
    # define R3/R4 execution symbols. Prove (a) the recursive walk reaches the recorder
    # lane, and (b) the maker forbid-scan's scope NEVER includes a live_recorder module,
    # so a forbidden symbol defined under veridex.live_recorder.* is permitted by
    # construction (the subpackage mutation check demonstrates this end-to-end).
    recorder_mods = _walk_modnames(live_recorder_pkg, "veridex.live_recorder.")
    assert recorder_mods, "recursive walk must reach live_recorder modules"

    maker_scanned = set(_walk_modnames(maker_pkg, "veridex.maker."))
    assert maker_scanned, "recursive maker scan must find modules"
    assert not any(m.startswith("veridex.live_recorder") for m in maker_scanned), (
        "the maker forbid-scan must never include a live_recorder module"
    )


def test_assign_rung_never_r3_r4() -> None:
    # Even with EVERY presence flag set True, the gate caps at MM-R1.5 — R3/R4
    # are recorder-lane-only (depth/cancels/own-fills are accepted but ignored here).
    presence = DataPresence(
        has_mids=True,
        has_trades=True,
        has_fill_assumption=True,
        has_l2_depth=True,
        has_cancels=True,
        has_own_fills=True,
    )
    assert assign_rung(presence) not in (MakerRungLabel.R3, MakerRungLabel.R4)
