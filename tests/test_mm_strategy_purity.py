"""Pure-tier purity guard (REQ-002 / AC-031 / RED-20 / RED-47, §6.3(1),(3)).

The MM-R4-B strategy tier ``veridex.mm_strategy.{contracts,config,basis,core}`` MUST stay
DETERMINISTIC and dependency-isolated: it may import ONLY stdlib + pydantic +
``veridex.mm_strategy.contracts`` + ``veridex.runtime.evidence`` (the shared canonical
serializer). Nothing else from ``veridex.*`` — no ``dust_execution`` / ``live_recorder`` /
``venues`` / ``maker`` / ``scoring`` / ``research`` — and no LLM SDK.

This guard enforces that exact whitelist two ways, mirroring the fresh-subprocess technique in
``tests/test_dust_execution_sec_isolation.py`` and the AST import-walk in
``tests/test_no_r3_r4_code.py``:

1. ``test_pure_tier_ast_imports_only_whitelist`` — a STATIC AST scan of each pure module's
   source: every imported module path must be ⊆ {stdlib, ``pydantic``,
   ``veridex.mm_strategy.contracts``, ``veridex.runtime.evidence``}.
2. ``test_pure_tier_fresh_process_exact_whitelist`` — a FRESH-SUBPROCESS runtime audit: in a
   clean interpreter, importing the four pure modules AND running the real ``core.decide()``
   over a fixture must leave ``sys.modules ∩ veridex.*`` ⊆ the allowed runtime set, checked
   BOTH at import time AND POST-``decide()``. The runtime audit closes the static-AST ceiling
   (a computed/import-TIME dynamic import a static scan cannot resolve).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import StrategyObservation, StrategyState

# The four PURE modules under guard (the package ``__init__`` is empty and not decision code).
_PURE_MODULES = (
    "veridex.mm_strategy.contracts",
    "veridex.mm_strategy.config",
    "veridex.mm_strategy.basis",
    "veridex.mm_strategy.core",
)

# STATIC-AST whitelist: the only ``veridex.*`` module PATHS a pure module may import FROM.
_AST_ALLOWED_VERIDEX_IMPORTS = frozenset(
    {"veridex.mm_strategy.contracts", "veridex.runtime.evidence"}
)

# RUNTIME whitelist: the only ``veridex.*`` entries allowed resident in ``sys.modules`` after a
# fresh-interpreter import of the pure tier + a ``decide()`` — the four pure modules, the
# ``veridex`` / ``veridex.mm_strategy`` / ``veridex.runtime`` packages they live under, and the
# ``veridex.runtime.evidence`` serializer.
_RUNTIME_ALLOWED_VERIDEX_MODULES = frozenset(
    {
        "veridex",
        "veridex.mm_strategy",
        "veridex.runtime",
        "veridex.runtime.evidence",
        *_PURE_MODULES,
    }
)


def _purity_decide_fixture() -> tuple[StrategyObservation, StrategyState, StrategyConfig]:
    """NAMED, LOAD-BEARING SEAM: build a VALID ``(observation, state, config)`` triple that the
    real ``veridex.mm_strategy.core.decide()`` accepts.

    Later tasks (E2-T3/E2-T4) that replace ``core.decide`` MUST keep this factory producing a
    triple ``decide()`` accepts — it is the single fixture both this in-process test and the
    fresh-subprocess audit drive ``decide()`` with, so the purity guard keeps exercising the
    REAL decision path, not a re-implementation.
    """
    return (
        StrategyObservation(token_id="TOKEN-YES", ts=1_000),
        StrategyState(tick_seq=0),
        StrategyConfig(strategy_id="mm-skeleton", enabled=True),
    )


# --- (1) Static AST import-whitelist ------------------------------------------------------


def _imported_module_paths(modname: str) -> set[str]:
    """Every module PATH imported by ``modname`` (AST — code only, so a prose mention in a
    docstring/comment can never trip the bar).

    For ``import a.b`` the dotted target is the path; for ``from a.b import x`` the module
    imported FROM (``a.b``) is the dependency (relative forms resolve against the package).
    """
    mod = importlib.import_module(modname)
    package = getattr(mod, "__package__", "") or ""
    tree = ast.parse(inspect.getsource(mod))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                try:
                    base = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                except (ImportError, ValueError):
                    base = node.module or ""
            else:
                base = node.module or ""
            if base:
                paths.add(base)
    return paths


def _is_whitelisted_import(path: str) -> bool:
    """True iff ``path`` is stdlib, ``pydantic``, or an allowed ``veridex.*`` module."""
    top = path.split(".")[0]
    if top == "veridex":
        return path in _AST_ALLOWED_VERIDEX_IMPORTS
    if top == "pydantic":
        return True
    return top in sys.stdlib_module_names


def test_pure_tier_ast_imports_only_whitelist() -> None:
    # Static ceiling: every import in every pure module resolves to stdlib / pydantic / the two
    # allowed veridex modules. A stray ``veridex.dust_execution`` (etc.) import fails HERE.
    offenders: dict[str, list[str]] = {}
    for modname in _PURE_MODULES:
        bad = sorted(
            p for p in _imported_module_paths(modname) if not _is_whitelisted_import(p)
        )
        if bad:
            offenders[modname] = bad
    assert not offenders, (
        "pure-tier modules may import ONLY stdlib + pydantic + veridex.mm_strategy.contracts "
        f"+ veridex.runtime.evidence; found non-whitelisted imports: {offenders}"
    )


# --- (2) Fresh-subprocess exact-whitelist runtime audit -----------------------------------
# Runs in a FRESH interpreter (cwd = repo root, so ``veridex`` and ``tests`` are importable):
# imports the four pure modules, snapshots ``sys.modules ∩ veridex.*`` at IMPORT time, then
# imports the NAMED ``_purity_decide_fixture`` + the real ``decide`` and runs it, and snapshots
# AGAIN POST-``decide()``. Any veridex module outside the runtime whitelist at EITHER point is an
# offender → exit 3 with a JSON ``{"import": [...], "post": [...]}`` payload on stdout. Because the
# fixture is imported from THIS test module, the module's own top-level imports are deliberately
# kept within the whitelist so the harness never flags itself. Offline: no creds/network.
_PURITY_AUDIT_SCRIPT = """
import importlib, json, sys

payload = json.loads(sys.stdin.read())
pure = payload["pure"]
allowed = set(payload["allowed"])


def _veridex_resident():
    return sorted(m for m in sys.modules if m == "veridex" or m.startswith("veridex."))


for _name in pure:
    importlib.import_module(_name)
import_offenders = [m for m in _veridex_resident() if m not in allowed]

from tests.test_mm_strategy_purity import _purity_decide_fixture
from veridex.mm_strategy.core import decide

_obs, _state, _config = _purity_decide_fixture()
decide(_obs, _state, _config)
post_offenders = [m for m in _veridex_resident() if m not in allowed]

if import_offenders or post_offenders:
    sys.stdout.write(json.dumps({"import": import_offenders, "post": post_offenders}))
    sys.exit(3)
sys.exit(0)
"""


def _run_purity_audit(
    pure: tuple[str, ...], allowed: frozenset[str]
) -> tuple[int, str, str]:
    """Import ``pure`` + run ``decide()`` in a FRESH interpreter; report whitelist offenders.

    Returns ``(returncode, stdout, stderr)``: rc==0 means no offending veridex module was
    resident at import time OR post-decide; rc==3 means at least one was, and stdout is the
    JSON ``{"import": [...], "post": [...]}`` payload naming them.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _PURITY_AUDIT_SCRIPT],
        input=json.dumps({"pure": list(pure), "allowed": sorted(allowed)}),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_pure_tier_fresh_process_exact_whitelist() -> None:
    # Runtime backstop past the static-AST ceiling: a clean interpreter imports the pure tier and
    # runs the REAL decide() over the load-bearing fixture; NO veridex module outside the runtime
    # whitelist may be resident — at import time OR after decide(). The real pure tier depends on
    # nothing forbidden → rc==0.
    rc, out, err = _run_purity_audit(_PURE_MODULES, _RUNTIME_ALLOWED_VERIDEX_MODULES)
    assert rc == 0, (
        "importing the pure tier + running decide() in a clean interpreter must load NO veridex "
        f"module outside the whitelist.\nstdout={out!r}\nstderr={err!r}"
    )
