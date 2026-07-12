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

import ast
import importlib
import sys
from pathlib import Path

import veridex.dust_execution
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


# Out-of-scope surfaces (CON scope-fence, §6 group 15) matched CASE-INSENSITIVELY as substrings.
# ``_IDENT`` covers real identifiers (Name/Attribute/class/func names); ``_IMPORT`` additionally
# forbids the vendored beta-SDK package path marker ``_vendor`` — the dust lane is a copy, never an
# import of that client. Kept a FOCUSED denylist on purpose: Phase 2E owns generalization.
_FORBIDDEN_IDENT_SUBSTRINGS = ("combo", "rfq", "relayer", "crosschain", "cross_chain")
_FORBIDDEN_IMPORT_SUBSTRINGS = _FORBIDDEN_IDENT_SUBSTRINGS + ("_vendor",)


def _forbidden_scope_symbols(pkg_dir: Path) -> list[str]:
    """AST scan of every ``*.py`` under ``pkg_dir`` for out-of-scope Combo/RFQ/Relayer/cross-chain/
    beta-SDK surfaces referenced as REAL CODE.

    Inspects only real code nodes — ``import``/``from ... import`` module paths and imported/alias
    names, plus ``ast.Name``/``ast.Attribute``/class/function identifiers — and DELIBERATELY never
    ``ast.Constant`` (string literals/docstrings); comments are absent from the AST entirely. So a
    prose disclaimer that merely NAMES an out-of-scope concept (as the real lane files do) cannot
    false-trip the fence. Returns a sorted ``"<file>:<node>:<symbol>"`` list for a readable assert.
    """
    hits: set[str] = set()

    def _flag(filename: str, node_kind: str, symbol: str, substrings: tuple[str, ...]) -> None:
        low = symbol.lower()
        if any(bad in low for bad in substrings):
            hits.add(f"{filename}:{node_kind}:{symbol}")

    for py in sorted(Path(pkg_dir).glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _flag(py.name, "import", alias.name, _FORBIDDEN_IMPORT_SUBSTRINGS)
                    if alias.asname:
                        _flag(py.name, "import-as", alias.asname, _FORBIDDEN_IMPORT_SUBSTRINGS)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    _flag(py.name, "from", node.module, _FORBIDDEN_IMPORT_SUBSTRINGS)
                for alias in node.names:
                    _flag(py.name, "from-name", alias.name, _FORBIDDEN_IMPORT_SUBSTRINGS)
                    if alias.asname:
                        _flag(py.name, "from-as", alias.asname, _FORBIDDEN_IMPORT_SUBSTRINGS)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                _flag(py.name, "def", node.name, _FORBIDDEN_IDENT_SUBSTRINGS)
            elif isinstance(node, ast.Name):
                _flag(py.name, "name", node.id, _FORBIDDEN_IDENT_SUBSTRINGS)
            elif isinstance(node, ast.Attribute):
                _flag(py.name, "attr", node.attr, _FORBIDDEN_IDENT_SUBSTRINGS)
            # ast.Constant (string literals / docstrings) is intentionally NOT inspected.
    return sorted(hits)


def test_no_combo_rfq_relayer_sdk_in_dust_execution(tmp_path: Path) -> None:
    # CON scope-fence (§6 group 15): veridex.dust_execution is the FOCUSED R4-A dust lane —
    # Phase 2E owns generalization to Combo/RFQ/Relayer/cross-chain and any vendored beta-SDK
    # client. This fence asserts NONE of those surfaces leak into the lane as REAL CODE
    # (imports + Name/Attribute/def identifiers). It is AST-based ON PURPOSE: the current lane
    # files carry prose disclaimers (e.g. contracts.py's "copy, not an import"), and a naive
    # grep would false-trip on any such docstring/comment. _forbidden_scope_symbols() inspects
    # only real code nodes and never string constants, so a mention in prose is invisible to it.
    pkg_dir = Path(veridex.dust_execution.__file__).parent

    # (1) GREEN on the real lane: zero out-of-scope surfaces referenced as code.
    real_hits = _forbidden_scope_symbols(pkg_dir)
    assert not real_hits, (
        "veridex.dust_execution must not reference combo/rfq/relayer/cross-chain/beta-SDK "
        f"(_vendor) surfaces as real code — Phase 2E owns generalization: {real_hits}"
    )

    # (2) Positive control (teeth): the same forbidden surfaces used as REAL CODE — a vendored
    # beta-SDK import path (_vendor), a combo client symbol, an rfq def, a relayer call — MUST
    # be reported. A fence that can catch nothing is worthless.
    pos = tmp_path / "pos"
    pos.mkdir()
    (pos / "leak.py").write_text(
        "from veridex.venues._vendor.polymarket_clob import combo_client\n"
        "\n"
        "def rfq_take():\n"
        "    return relayer_submit(combo_client)\n",
        encoding="utf-8",
    )
    pos_hits = _forbidden_scope_symbols(pos)
    assert pos_hits, "fence has no teeth: a REAL forbidden code symbol was not caught"

    # (3) Negative control (no prose false-positive): the ONLY mention of the forbidden surfaces
    # is inside a docstring and a comment. The AST fence MUST stay silent (it is the whole point
    # of scanning code, not text — mirrors the real lane's prose disclaimers).
    neg = tmp_path / "neg"
    neg.mkdir()
    (neg / "prose.py").write_text(
        '"""Combo/RFQ/Relayer/cross-chain and the beta-SDK _vendor client are OUT OF SCOPE.\n'
        "\n"
        'This is a copy, not an import (Phase 2E owns generalization)."""\n'
        "# combo rfq relayer crosschain cross_chain _vendor are named only in this comment\n"
        "SAFE_VALUE = 1\n",
        encoding="utf-8",
    )
    neg_hits = _forbidden_scope_symbols(neg)
    assert not neg_hits, (
        f"fence false-tripped on a docstring/comment mention (prose, not code): {neg_hits}"
    )


# =====================================================================================
# E1-T5 (SEC-006, AC-014/015): freeze + denylist the FULL R4-A execution field set so a
# forgotten field can never rank across all three rank surfaces (closes Codex-M7).
# =====================================================================================
import pytest
from veridex.rank_guards import R4A_EXECUTION_DENYLIST_FIELDS, R3_R4_RANK_DENYLIST
from veridex.scoring import _rank_key as dir_key
from veridex.leaderboard import _rank_key as clv_key
from veridex.maker.leaderboard import maker_rank_key
# NOTE (Fable-m5): dir_key/clv_key SUBSCRIPT required metric keys, so a bare {poison} raises KeyError,
# NOT the guard. Build a COMPLETE valid metrics row + the poisoned key so the guard is the ONLY reason.
VALID_DIR = {"avg_clv_bps": 0.0, "total_clv_bps": 0.0, "brier": 0.0, "max_drawdown": 0.0, "action_count": 0, "agent_id": "a"}
# Codex-M2/Fable-m1: parametrize + assert EXACT equality over an INDEPENDENT literal — NOT the
# production set. `<=` (subset) over the production set is inert: dropping a field shrinks BOTH the
# set AND the parametrization, so nothing fails. The independent literal is the ground truth.
EXPECTED_R4A_FIELDS = frozenset({"own_fill", "filled_size", "fill_price", "realized_pnl", "inventory",
                                 "real_fill_reconciliation", "post_trade_markout"})  # + any E1-T2/E2 field
@pytest.mark.parametrize("field", sorted(EXPECTED_R4A_FIELDS))
def test_all_three_surfaces_reject_every_r4a_field(field):
    for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
        with pytest.raises(AssertionError):          # the guard (AssertionError), not KeyError
            keyfn({**base, field: 1.0})
        with pytest.raises(AssertionError):          # sorted() bypass also raises
            sorted([{**base, field: 1.0}], key=keyfn)
def test_r4a_field_set_equals_expected_and_is_denylisted():
    assert R4A_EXECUTION_DENYLIST_FIELDS == EXPECTED_R4A_FIELDS   # EXACT equality vs independent literal → omission fails HERE
    assert EXPECTED_R4A_FIELDS <= R3_R4_RANK_DENYLIST             # and the canonical set is enforced
def test_real_executable_edge_bps_stays_excluded():
    assert dir_key({**VALID_DIR, "real_executable_edge_bps": None}) is not None  # does NOT raise
