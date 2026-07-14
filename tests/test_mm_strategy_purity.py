"""Pure-tier purity guard (REQ-002 / AC-031 / RED-20 / RED-47, §6.3(1),(3)).

The MM-R4-B strategy tier ``veridex.mm_strategy.{contracts,config,basis,core}`` MUST stay
DETERMINISTIC and dependency-isolated: it may import ONLY stdlib + pydantic + its OWN pure
siblings (``veridex.mm_strategy.{contracts,config,basis,core}`` — REQ-002/spec:69 permits the
pure tier to import each other) + ``veridex.runtime.evidence`` (the shared canonical serializer).
Nothing else from ``veridex.*`` — no ``dust_execution`` / ``live_recorder`` / ``venues`` /
``maker`` / ``scoring`` / ``research`` — and no LLM SDK.

This guard enforces that exact whitelist two ways, mirroring the fresh-subprocess technique in
``tests/test_dust_execution_sec_isolation.py`` and the AST import-walk in
``tests/test_no_r3_r4_code.py``:

1. ``test_pure_tier_ast_imports_only_whitelist`` — a STATIC AST scan of each pure module's
   source: every imported module path must be ⊆ {stdlib, ``pydantic``, the four pure
   ``veridex.mm_strategy`` siblings, ``veridex.runtime.evidence``}. A companion predicate test
   pins the positive control (a non-sibling ``veridex.*`` path still fails).
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
import tempfile
from pathlib import Path

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    StrategyObservation,
    StrategyState,
)

# The four PURE modules under guard (the package ``__init__`` is empty and not decision code).
_PURE_MODULES = (
    "veridex.mm_strategy.contracts",
    "veridex.mm_strategy.config",
    "veridex.mm_strategy.basis",
    "veridex.mm_strategy.core",
)

# STATIC-AST whitelist: the only ``veridex.*`` module PATHS a pure module may import FROM. The
# four pure siblings may import EACH OTHER (REQ-002, spec:69 — e.g. ``basis`` imports ``config``
# for the estimator selection), plus the shared ``veridex.runtime.evidence`` serializer. Nothing
# ELSE from ``veridex.*`` (no ``dust_execution`` / ``live_recorder`` / ``venues`` / …) — the
# positive control below pins that a non-sibling path still fails this predicate.
_AST_ALLOWED_VERIDEX_IMPORTS = frozenset({*_PURE_MODULES, "veridex.runtime.evidence"})

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
    observation = StrategyObservation(
        fixture_id=1,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id="TOKEN-YES",
        venue_market_ref="0xmarket",
        tick_size=0.01,
        observation_sequence=1,
        book_source_epoch=1,
        bid=0.49,
        ask=0.51,
        bid_size=100.0,
        ask_size=120.0,
        book_status="ok",
        status_reason=None,
        book_recv_ts=1_000,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=990,
        guard_fv=None,
        market_status="ACTIVE",
        market_status_recv_ts=995,
        market_status_epoch=1,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=1_000, fresh=True
        ),
        as_of_ts=1_000,
    )
    # A SEEDED mid-stream state (not a fresh/cold-start one), so ``decide()`` runs the COMPLETE
    # E2-T4 S/R/E/D/C/F/W/H reducer body — not just the cold-start baseline seed. This clean, healthy
    # frame classifies row W (references below ``ref_min_samples``), so the audit exercises the real
    # admission path (the ``basis`` smoother/reference reducers) in the fresh subprocess.
    state = StrategyState(
        last_observation_sequence=0,
        last_book_source_epoch=1,
        last_as_of_ts=999,
        last_market_status_epoch=1,
        last_market_status_recv_ts=990,
        smoother_mid=0.5,
        smoother_mid_ts=999,
        spread_ref_samples=(0.02,),
        depth_ref_samples=(100.0,),
    )
    return (
        observation,
        state,
        # ``guard_enabled`` is REQUIRED (no default) on the full StrategyConfig; supply it so the
        # fixture keeps constructing a valid config for the real ``decide()`` path.
        StrategyConfig(guard_enabled=True),
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
        "pure-tier modules may import ONLY stdlib + pydantic + their own pure "
        "veridex.mm_strategy siblings + veridex.runtime.evidence; found non-whitelisted "
        f"imports: {offenders}"
    )


def test_ast_whitelist_admits_pure_siblings_rejects_non_siblings() -> None:
    # Positive control for the widened whitelist: the predicate ADMITS every pure sibling and the
    # shared serializer, yet still REJECTS any other veridex.* path — so widening for the
    # sibling-import allowance (REQ-002) did NOT defeat the isolation guard. This is the static
    # counterpart to the basis.py mutation (temporarily importing veridex.live_recorder.* must
    # fail test_pure_tier_ast_imports_only_whitelist).
    for allowed in (*_PURE_MODULES, "veridex.runtime.evidence", "hashlib", "pydantic"):
        assert _is_whitelisted_import(allowed), f"{allowed} should be whitelisted"
    for forbidden in (
        "veridex.live_recorder.contracts",
        "veridex.dust_execution.analysis",
        "veridex.venues.polymarket_resolver",
        "veridex.runtime.something_else",
    ):
        assert not _is_whitelisted_import(forbidden), f"{forbidden} must NOT be whitelisted"


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


# --- (3) Assembler adapter-tier import-closure guard (E3-T5, §6.3(8) / RED-49) ------------
# The assembler is the ONE R4-B module that legitimately reaches the live-recorder READ/resume
# surfaces, so the trust boundary lives HERE (spec §6.3(8)): it may import stdlib + pydantic + the
# pure ``mm_strategy.*`` tier + the ``veridex.live_recorder.*`` read surfaces + AT MOST the two
# READ-only venue surfaces (``polymarket_resolver`` + ``market_status``) — and NOTHING that
# submits/cancels/signs/funds (``veridex.venues.base`` / ``veridex.venues.sx_bet`` / a local-key
# signer), no LLM SDK, and no ranked lane. This guard pins that closure; RED-49's positive control
# is transiently importing ``veridex.venues.sx_bet`` into the assembler (documented in the E3-T5
# report), and the in-suite synthetic control below keeps the audit biting in CI without that edit.

_ASSEMBLER_MODULE = "veridex.mm_strategy.assembler"

# The ONLY ``veridex.venues.*`` SUBMODULES the assembler may reach — the two READ-only surfaces
# (spec §6.3(8)). A SUBSET ceiling, not equality: the clean assembler imports ZERO venues today
# (∅ ⊆ allowed → green), yet ANY other ``veridex.venues.*`` — above all the submit/cancel
# ``base`` / ``sx_bet`` surfaces — is a trust-boundary breach.
_ALLOWED_ASSEMBLER_VENUE_MODULES = frozenset(
    {
        "veridex.venues.polymarket_resolver",
        "veridex.venues.market_status",
    }
)

# ``veridex.*`` prefixes the assembler must NEVER load: the submit/cancel venue ``base`` + the SX
# submit adapter, and the ranked maker/scoring/leaderboard lane (spec names the ranked lane
# ``veridex.research.*``; this repo's concrete ranked surfaces are ``veridex.maker`` / ``scoring`` /
# ``leaderboard`` — both are forbidden so the guard bites whichever name a regression uses).
_FORBIDDEN_ASSEMBLER_VERIDEX_PREFIXES = (
    "veridex.venues.base",
    "veridex.venues.sx_bet",
    "veridex.maker",
    "veridex.scoring",
    "veridex.leaderboard",
    "veridex.research",
)

# Third-party surfaces forbidden in the adapter closure: LLM SDKs (the assembler is NOT an LLM lane)
# + local-key signer / EIP-712 / credential libraries (the assembler NEVER signs or funds). Matched
# on the top-level distribution name of an import.
_FORBIDDEN_ASSEMBLER_THIRDPARTY = frozenset(
    {
        "anthropic",
        "openai",
        "eth_account",
        "eth_keys",
        "coincurve",
        "web3",
    }
)

# Bare signer/credential SYMBOLS forbidden in the assembler's import surface (a ``from ... import``
# that pulls a signer type without its top-level dist name showing as an import path). Mirrors the
# ``_NO_LOCAL_KEY_BANNED`` set in ``tests/test_dust_execution_sec_isolation.py``.
_FORBIDDEN_ASSEMBLER_SIGNER_SYMBOLS = frozenset({"UtilsSigner", "Account"})


def _assembler_imported_surface() -> set[str]:
    """Every module PATH and imported/alias SYMBOL name in the assembler (AST — code only).

    Inspects only ``import`` / ``from ... import`` nodes, so the module docstring's prose mention
    of ``veridex.venues.base`` / ``sx_bet`` (it documents the boundary it forbids) can NEVER
    false-trip the bar — a docstring is an ``ast.Constant``, never an import node.
    """
    mod = importlib.import_module(_ASSEMBLER_MODULE)
    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
    return names


# Fresh-interpreter transitive-closure audit (mirrors ``_run_ranked_import_audit`` in
# ``tests/test_dust_execution_sec_isolation.py``): import ``module`` in a clean process (cwd=repo
# root so ``veridex`` is importable), then scan the REAL resident ``sys.modules`` graph — not just
# the top-of-file AST — for any forbidden ``veridex.*`` prefix, any ``veridex.venues.*`` submodule
# outside the READ-only pair, or any forbidden third-party top-level dist. A transitive pull of
# ``sx_bet`` via ANY intermediary is caught here even when the top-level AST is clean. Offline: no
# creds / network. Offenders → exit 3 with a JSON list on stdout.
_ASSEMBLER_CLOSURE_AUDIT_SCRIPT = """
import importlib, json, sys

payload = json.loads(sys.stdin.read())
for _extra in payload.get("sys_path", []):
    sys.path.insert(0, _extra)

module = payload["module"]
allowed_venues = set(payload["allowed_venues"])
forbidden_prefixes = tuple(payload["forbidden_prefixes"])
forbidden_thirdparty = set(payload["forbidden_thirdparty"])

importlib.import_module(module)


def _under(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


offenders = []
for name in sorted(sys.modules):
    top = name.split(".")[0]
    if top == "veridex":
        if any(_under(name, p) for p in forbidden_prefixes):
            offenders.append(name)
        elif name.startswith("veridex.venues.") and name not in allowed_venues:
            offenders.append(name)
    elif top in forbidden_thirdparty:
        offenders.append(name)

if offenders:
    sys.stdout.write(json.dumps(sorted(set(offenders))))
    sys.exit(3)
sys.exit(0)
"""


def _run_assembler_closure_audit(
    module: str, sys_path: list[str] | None = None
) -> tuple[int, str, str]:
    """Import ``module`` in a FRESH interpreter and report import-closure offenders.

    Returns ``(returncode, stdout, stderr)``: rc==0 means the transitive ``sys.modules`` graph held
    NO forbidden veridex prefix, no stray ``veridex.venues.*``, and no forbidden third-party dist;
    rc==3 means at least one was resident and stdout is the JSON list naming them.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ASSEMBLER_CLOSURE_AUDIT_SCRIPT],
        input=json.dumps(
            {
                "module": module,
                "sys_path": list(sys_path or []),
                "allowed_venues": sorted(_ALLOWED_ASSEMBLER_VENUE_MODULES),
                "forbidden_prefixes": list(_FORBIDDEN_ASSEMBLER_VERIDEX_PREFIXES),
                "forbidden_thirdparty": sorted(_FORBIDDEN_ASSEMBLER_THIRDPARTY),
            }
        ),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_assembler_import_closure() -> None:
    # (a) STATIC AST ceiling over the assembler's OWN import block. Every forbidden surface is
    # asserted absent from the source imports; a docstring mention (the module documents the
    # boundary) is an ast.Constant, not an import, so it never trips this.
    surface = _assembler_imported_surface()
    paths = _imported_module_paths(_ASSEMBLER_MODULE)

    forbidden_paths = sorted(
        p
        for p in paths
        if any(
            p == pre or p.startswith(pre + ".")
            for pre in _FORBIDDEN_ASSEMBLER_VERIDEX_PREFIXES
        )
    )
    assert not forbidden_paths, (
        "assembler must NOT import the submit/cancel venue base/sx_bet or the ranked "
        f"maker/scoring/leaderboard/research lane; found: {forbidden_paths}"
    )

    stray_venues = sorted(
        p
        for p in paths
        if p.startswith("veridex.venues.")
        and p not in _ALLOWED_ASSEMBLER_VENUE_MODULES
    )
    assert not stray_venues, (
        "assembler venue imports must be a subset of the two READ-only surfaces "
        f"{sorted(_ALLOWED_ASSEMBLER_VENUE_MODULES)}; found extra venue imports: {stray_venues}"
    )

    banned_surface = sorted(
        (surface & _FORBIDDEN_ASSEMBLER_THIRDPARTY)
        | (surface & _FORBIDDEN_ASSEMBLER_SIGNER_SYMBOLS)
    )
    assert not banned_surface, (
        "assembler must NOT import any LLM SDK or local-key signer/EIP-712/credential surface; "
        f"found: {banned_surface}"
    )

    # (b) FRESH-PROCESS transitive closure: the REAL resident sys.modules graph after importing the
    # assembler in a clean interpreter holds no forbidden veridex prefix, no stray venue submodule,
    # and no forbidden third-party dist — catching a forbidden surface pulled in TRANSITIVELY that
    # the top-level AST in (a) cannot see. The real assembler is clean → rc==0.
    rc, out, err = _run_assembler_closure_audit(_ASSEMBLER_MODULE)
    assert rc == 0, (
        "importing the assembler in a clean interpreter must load NO forbidden venue/signer/LLM/"
        f"ranked surface (transitive closure).\nstdout={out!r}\nstderr={err!r}"
    )

    # (c) POSITIVE CONTROL (permanent in-suite teeth): the SAME fresh-process audit, pointed at a
    # synthetic adapter that reaches the forbidden SX submit surface, MUST fail and NAME it — so the
    # green (a)/(b) prove a CLEAN closure, not a toothless check. (RED-49's manual counterpart is
    # transiently importing sx_bet into assembler.py itself; this keeps the guard biting without it.)
    with tempfile.TemporaryDirectory() as tmp:
        breach = Path(tmp) / "adv_assembler_breaches_sx.py"
        breach.write_text(
            "from veridex.venues.sx_bet import SXBetAdapter  # noqa: F401\n",
            encoding="utf-8",
        )
        rc_bad, out_bad, err_bad = _run_assembler_closure_audit(
            "adv_assembler_breaches_sx", sys_path=[tmp]
        )
    assert rc_bad == 3, (
        "assembler import-closure audit has no teeth: a synthetic adapter importing "
        f"veridex.venues.sx_bet was NOT flagged.\nstdout={out_bad!r}\nstderr={err_bad!r}"
    )
    assert "veridex.venues.sx_bet" in out_bad, (
        f"audit failed to name the forbidden SX submit surface it should have caught: {out_bad!r}"
    )
