"""SEC-003/AC-039 isolation guard: ranked lanes never depend on ``veridex.dust_execution``.

R4-A dust execution/safety records REAL fills and lives in its own package
``veridex.dust_execution`` — distinct from the COUNTERFACTUAL-only ``veridex.live_recorder``
lane and forbidden from the ranked maker/scoring/leaderboard lanes. This module proves the
import boundary is enforceable (static AST scan) AND observed at runtime.

STATIC-AST CEILING (honest scope of ``_imports_module``): the AST bar matches every
*statically resolvable* import form — plain/aliased ``import``, ``from ... import``,
``from veridex import dust_execution``, relative imports resolving into the target, and
dynamic ``importlib.import_module("…")`` / ``__import__("…")`` with a STRING-CONSTANT
argument. It CANNOT see a dynamic import whose module name is a non-constant expression
(computed at runtime). To keep the AC-039 "no ranked lane depends on dust_execution" claim
true past that ceiling, ``test_ranked_import_leaves_no_dust_execution_in_sys_modules`` adds a
runtime module-dependency assertion: importing each ranked module must not pull
``veridex.dust_execution`` into ``sys.modules``.
"""

import importlib
import sys

import veridex.maker

from tests.test_no_r3_r4_code import _imports_module, _walk_modnames


def test_maker_scoring_do_not_import_dust_execution() -> None:
    modnames = _walk_modnames(veridex.maker, "veridex.maker.") + [
        "veridex.scoring",
        "veridex.leaderboard",
    ]
    offenders = [m for m in modnames if _imports_module(m, "veridex.dust_execution")]
    assert not offenders, f"ranked lanes must not import veridex.dust_execution: {offenders}"


def test_ranked_import_leaves_no_dust_execution_in_sys_modules() -> None:
    # Runtime backstop past the static-AST ceiling: importing a ranked module must never
    # (transitively) load veridex.dust_execution. Catches dynamic imports whose target is a
    # non-constant expression that the AST bar cannot resolve.
    modnames = _walk_modnames(veridex.maker, "veridex.maker.") + [
        "veridex.scoring",
        "veridex.leaderboard",
    ]
    # Clear the ENTIRE veridex.dust_execution namespace (package + any submodules a sibling
    # test already imported at collection time) so the post-import check attributes any (re)load
    # PURELY to the ranked-lane imports below — not to unrelated test-ordering pollution. Popping
    # only the package left sibling-loaded submodules (e.g. .contracts/.manifest) resident, which
    # misfired this guard in the full milestone suite. A clean slate is also stronger teeth: any
    # reappearance below is attributable ONLY to a ranked-lane import, even if a sibling had it.
    for _dust_mod in [m for m in list(sys.modules) if m == "veridex.dust_execution" or m.startswith("veridex.dust_execution.")]:
        sys.modules.pop(_dust_mod, None)
    for modname in modnames:
        importlib.import_module(modname)
    dust_loaded = [m for m in sys.modules if m == "veridex.dust_execution" or m.startswith("veridex.dust_execution.")]
    assert not dust_loaded, (
        f"importing ranked lanes must not load veridex.dust_execution at runtime: {dust_loaded}"
    )
