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
(computed at runtime) — including one performed at import TIME in a module body. To keep the
AC-039 "no ranked lane depends on dust_execution" claim true past that ceiling,
``test_ranked_import_in_fresh_interpreter_loads_no_dust_execution`` runs a FRESH-SUBPROCESS
module-dependency audit: in a clean interpreter where NEITHER the ranked lanes NOR
``veridex.dust_execution`` are preloaded, importing the ranked modules re-executes every module
body and must not pull ``veridex.dust_execution`` into ``sys.modules``. This closes the
Gate#1 MAJOR-3 hole where the earlier in-process backstop re-imported already-CACHED ranked
modules (bodies never re-ran), so an import-TIME computed dynamic import was never re-observed.
"""

import ast
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


# --- Fresh-subprocess ranked-import audit (Gate#1 MAJOR-3) ---------------------------------
# The earlier in-process backstop popped only veridex.dust_execution* from sys.modules and then
# re-imported ranked modules that were STILL CACHED — their bodies never re-executed, so a
# module-level (import-TIME) computed dynamic import of dust_execution was never re-observed. An
# import-TIME dependency therefore passed BOTH the static AST bar AND that runtime check. The
# audit script below runs in a FRESH interpreter where NEITHER ranked NOR dust modules are
# preloaded: importing the ranked lanes genuinely re-executes every module body, so any transitive
# (even import-TIME, even computed) veridex.dust_execution load is observed. It reads a JSON
# payload {sys_path, modules} on stdin, imports the modules, and exits 3 (naming the offenders on
# stdout) if any veridex.dust_execution* is resident, else 0. Offline: no creds/network/scrubbing.
_RANKED_IMPORT_AUDIT = """
import importlib, json, sys

payload = json.loads(sys.stdin.read())
for _extra in payload.get("sys_path", []):
    sys.path.insert(0, _extra)
for _name in payload["modules"]:
    importlib.import_module(_name)
_dust = sorted(
    m for m in sys.modules
    if m == "veridex.dust_execution" or m.startswith("veridex.dust_execution.")
)
if _dust:
    sys.stdout.write(json.dumps(_dust))
    sys.exit(3)
sys.exit(0)
"""


def _ranked_modnames() -> list[str]:
    # Same enumeration as the static bar: recursive walk of veridex.maker.* plus the two
    # directional rank surfaces (scoring, leaderboard).
    return _walk_modnames(veridex.maker, "veridex.maker.") + [
        "veridex.scoring",
        "veridex.leaderboard",
    ]


def _run_ranked_import_audit(modules: list[str], sys_path: list[str] | None = None) -> tuple[int, str]:
    """Import ``modules`` in a FRESH interpreter (cwd=repo root so ``veridex`` is importable via the
    ``-c`` empty ``sys.path[0]``) and report whether veridex.dust_execution* got loaded.

    Returns ``(returncode, stdout)``: rc==0 means no dust_execution load; rc==3 means it was loaded
    and stdout is the JSON list of offending module names.
    """
    import json as _json
    import subprocess as _subprocess

    proc = _subprocess.run(
        [sys.executable, "-c", _RANKED_IMPORT_AUDIT],
        input=_json.dumps({"modules": list(modules), "sys_path": list(sys_path or [])}),
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def test_ranked_import_in_fresh_interpreter_loads_no_dust_execution() -> None:
    # Runtime backstop past the static-AST ceiling, HARDENED (Gate#1 MAJOR-3): a fresh interpreter
    # imports ONLY the real ranked lanes; none may pull veridex.dust_execution into sys.modules —
    # including via an import-TIME computed dynamic import that the old cached in-process check and
    # the static AST bar both miss. The real lanes genuinely do not depend on dust_execution → rc==0.
    rc, out = _run_ranked_import_audit(_ranked_modnames())
    assert rc == 0, (
        "importing ranked lanes in a clean interpreter must not load veridex.dust_execution; "
        f"audit reported: {out}"
    )


def test_fresh_subprocess_audit_catches_import_time_computed_dust(tmp_path: Path) -> None:
    # POSITIVE MUTATION / teeth (Gate#1 MAJOR-3): a ranked-like module whose BODY performs an
    # import-TIME *computed* (non-constant) import of veridex.dust_execution evades BOTH the static
    # AST bar AND the old cached in-process runtime check. The fresh-subprocess audit MUST catch it.
    adv = tmp_path / "adv_ranked_import_time_dust.py"
    adv.write_text(
        "import importlib\n"
        "# import-TIME computed (non-constant) target: invisible to the static AST bar\n"
        'target = ".".join(("veridex", "dust_execution"))\n'
        "importlib.import_module(target)\n",
        encoding="utf-8",
    )

    # (a) Document the ceiling: the static AST bar does NOT resolve the computed import-TIME target.
    sys.path.insert(0, str(tmp_path))
    try:
        assert not _imports_module("adv_ranked_import_time_dust", "veridex.dust_execution"), (
            "static AST bar unexpectedly resolved a computed import-TIME target"
        )
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("adv_ranked_import_time_dust", None)

    # (b) The mutation: the fresh-subprocess audit re-executes the module body in a clean
    # interpreter, observes the import-TIME dust load, and FAILS (non-zero), naming the offender.
    rc, out = _run_ranked_import_audit(["adv_ranked_import_time_dust"], sys_path=[str(tmp_path)])
    assert rc != 0, "fresh-subprocess audit failed to catch an import-TIME computed dust import"
    assert "veridex.dust_execution" in out, f"audit did not name the offending module: {out!r}"


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
#
# GENUINELY INDEPENDENT hand-classification (Gate#1 MAJOR-4): built by walking EVERY field of
# EVERY event in dust_execution/contracts.py and classifying it as a realized-execution
# OUTCOME/DIAGNOSTIC (deny) vs envelope/join/intent/config/label/operational (allow). This is the
# ground-truth set the production `R4A_EXECUTION_DENYLIST_FIELDS` must EQUAL — never a copy of it.
# Each entry is annotated with its source event so the classification is auditable field-by-field.
EXPECTED_R4A_FIELDS = frozenset({
    # --- OrderStatusEvent (contracts.py) — the fill-lifecycle outcome ---
    "status",         # partial/filled/rejected/expired/unresolved — did/what a fill did
    "filled_size",    # matched size
    "fill_price",     # matched price (also OwnFillEvent)
    # --- OwnFillEvent — a realized own fill ---
    "fill_size",      # realized fill magnitude
    "fill_ts",        # fill timestamp = evidence a fill occurred
    # --- OrderCancelEvent — its SOLE outcome field ---
    "canceled",       # without it the whole cancel dump leaks
    # --- OrderAckEvent — its sole outcome field ---
    "ack_status",     # venue ack outcome
    # --- RealFillReconciliation — reconciliation outcome ---
    "reconciled_state",      # tri-state reconciliation verdict
    "reconciled_fill_size",  # reconciled realized size
    # --- InventoryEvent — net position diagnostic (AC-015) ---
    "net_inventory",
    # --- PostTradeMarkoutEvent — markout diagnostic (AC-014) ---
    "reference_price",  # markout reference (Gate#1 MAJOR-4 REVERSES E1-T5's exclusion: it leaks a markout diagnostic)
    "markout_bps",      # the markout value
    # --- SessionRiskSnapshot — realized-loss (PnL) magnitudes ---
    "realized_loss_session",
    "realized_loss_daily",
    # --- canonical rank-input CONCEPT aliases (defense-in-depth, not contract attr names) ---
    "own_fill",
    "realized_pnl",
    "inventory",
    "real_fill_reconciliation",
    "post_trade_markout",
})
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


# --- PRIMARY model-derived proof (Gate#1 MAJOR-4): reject REAL event dumps, not a hand literal ---
# For each realized-execution OUTCOME event, build a VALID instance, model_dump() it, merge the dump
# into a COMPLETE valid rank-metrics row, and assert ALL THREE rank surfaces raise (incl. the direct
# sorted(..., key=...) bypass). This proves rejection against the actual contract wire shape — so a
# NEW outcome field that the hand literal forgot is still caught here the moment it enters a dump.
from veridex.dust_execution.contracts import (  # noqa: E402
    OrderCancelEvent,
    OrderAckEvent,
    OrderStatusEvent,
    OwnFillEvent as _OwnFillEvent,
    RealFillReconciliation as _RealFillReconciliation,
    InventoryEvent,
    PostTradeMarkoutEvent,
)


def _outcome_event_dumps() -> list[tuple[str, dict]]:
    """Every realized-execution OUTCOME event, as its real ``model_dump()`` rank-row payload.

    Includes the ``PostTradeMarkoutEvent`` MINUS ``markout_bps`` case (Codex MAJOR-4 repro): with
    the obvious markout value removed, ``reference_price`` ALONE must still be rejected.
    """
    cancel = OrderCancelEvent(sequence_no=1, event_type="OrderCancelEvent", source_ts=None,
                              recv_ts=1000, decision_id="d1", client_order_id="c1",
                              venue_order_id="v1", canceled=True)
    ack = OrderAckEvent(sequence_no=1, event_type="OrderAckEvent", source_ts=None, recv_ts=1000,
                        decision_id="d1", client_order_id="c1", venue_order_id="v1",
                        ack_status="matched")
    ostatus = OrderStatusEvent(sequence_no=1, event_type="OrderStatusEvent", source_ts=None,
                               recv_ts=1000, decision_id="d1", client_order_id="c1",
                               venue_order_id="v1", status="partial", filled_size=1.0,
                               fill_price=0.5)
    own = _OwnFillEvent(sequence_no=1, event_type="OwnFillEvent", source_ts=None, recv_ts=1000,
                        decision_id="d1", client_order_id="c1", venue_order_id="v1", side="buy",
                        fill_price=0.6, fill_size=1.0, fill_ts=1000)
    recon = _RealFillReconciliation(sequence_no=1, event_type="RealFillReconciliation",
                                    source_ts=None, recv_ts=1000, decision_id="d1",
                                    venue_order_key="vk1", reconciled_state="RESOLVED",
                                    reconciled_fill_size=1.0)
    inv = InventoryEvent(sequence_no=1, event_type="InventoryEvent", source_ts=None, recv_ts=1000,
                         token_id="t1", net_inventory=5.0)
    markout = PostTradeMarkoutEvent(sequence_no=1, event_type="PostTradeMarkoutEvent",
                                    source_ts=None, recv_ts=1000, decision_id="d1", horizon_ms=100,
                                    reference_price=0.5, markout_bps=10.0)
    markout_no_bps = markout.model_dump()
    del markout_no_bps["markout_bps"]  # Codex repro: reference_price ALONE must still be rejected
    return [
        ("OrderCancelEvent", cancel.model_dump()),
        ("OrderAckEvent", ack.model_dump()),
        ("OrderStatusEvent", ostatus.model_dump()),
        ("OwnFillEvent", own.model_dump()),
        ("RealFillReconciliation", recon.model_dump()),
        ("InventoryEvent", inv.model_dump()),
        ("PostTradeMarkoutEvent", markout.model_dump()),
        ("PostTradeMarkoutEvent-minus-markout_bps", markout_no_bps),
    ]


@pytest.mark.parametrize("event_name,dump", _outcome_event_dumps(), ids=[n for n, _ in _outcome_event_dumps()])
def test_complete_outcome_event_dumps_rejected_by_all_rank_surfaces(event_name, dump):
    # Merge the REAL event dump into a complete valid rank row; the guard runs FIRST on every
    # surface, so an outcome field in the dump raises AssertionError (not KeyError) on all three
    # — including the raw sorted(..., key=...) path an attacker might use to bypass a wrapper.
    for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
        row = {**base, **dump}
        with pytest.raises(AssertionError):
            keyfn(row)
        with pytest.raises(AssertionError):
            sorted([row], key=keyfn)


# =====================================================================================
# E1-T6 (SEC-004, AC-016, §6 group 9): the sealed maker/scoring output is BYTE-IDENTICAL
# after exercising the R4-A dust-execution lane.
#
# Mirrors test_live_recorder_sec005.py:230-249 (the R3 recorder analogue): capture
# score_run(run) before, construct + use REAL R4-A lifecycle contracts, capture score_run
# after, and assert byte-identity via the shared veridex.maker.result.assert_score_run_untouched
# (imported, not re-defined). Constructing/using the R4-A contracts must not perturb the
# sealed directional leaderboard, and the sealed maker-arena-result.json on disk must remain
# byte-identical to its committed content. Being a "nothing changed" preservation guard it
# PASSES when the system is correct; its teeth are proven by the mutation checks (see task).
# =====================================================================================
import hashlib  # noqa: E402
import subprocess  # noqa: E402

from veridex.dust_execution.contracts import (  # noqa: E402
    DustExecutionSessionMeta,
    OwnFillEvent,
    RealFillReconciliation,
)
from veridex.maker.result import assert_score_run_untouched  # noqa: E402
from veridex.runtime.orchestrator import RunResult  # noqa: E402
from veridex.scoring import score_run  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEALED_MAKER_RESULT = "scripts/txline_live/cp1/maker-arena-result.json"


def _dir_row_e1t6(agent_id: str, tick_seq: int, clv_bps: int, confidence: float | None = None) -> dict:
    """A directional score row in the shape score_run consumes (mirrors sec005._dir_row)."""
    params: dict = {"market_key": "OU_2_5", "side": "over"}
    if confidence is not None:
        params["confidence"] = confidence
    return {
        "agent_id": agent_id,
        "tick_seq": tick_seq,
        "clv_bps": clv_bps,
        "valid": True,
        "reason": "",
        "raw_prescore": {"raw_action": {"type": "FLAG_VALUE", "params": params}},
    }


def _representative_run_e1t6() -> RunResult:
    """A representative directional run (mirrors sec005._representative_run)."""
    rows = [
        _dir_row_e1t6("agent-alpha", 0, 15, confidence=0.7),
        _dir_row_e1t6("agent-alpha", 1, -5, confidence=0.4),
        _dir_row_e1t6("agent-beta", 0, 8, confidence=0.55),
        _dir_row_e1t6("agent-beta", 1, 3),
    ]
    return RunResult(
        run_id="run-e1t6",
        source_mode="replay",
        agent_ids=["agent-alpha", "agent-beta"],
        run_events=[],
        score_rows=rows,
        evidence_hash="",
        proof_mode_map={"agent-alpha": "reproducible", "agent-beta": "reproducible"},
    )


def _simulate_r4a_dust_execution_session() -> None:
    """Exercise the R4-A dust-execution lane: build the session identity + a realized OWN fill +
    its reconciliation against complete venue truth (the REAL fill-carrying contracts that are the
    whole point of the lane). Constructing/using these must not touch the directional scorer."""
    DustExecutionSessionMeta(
        session_id="dust-sess-e1t6",
        mode="dry_run",
        wallet_ref="wallet-ref-e1t6",
        manifest_hash="m" * 64,
        policy_hash="p" * 64,
        caps_snapshot={"max_notional": 5.0, "max_order": 1.0},
        market_fee_snapshot_hash="f" * 64,
        operator_authorization_ref="op-auth-e1t6",
    )
    OwnFillEvent(
        sequence_no=1,
        event_type="OwnFillEvent",
        source_ts=None,
        recv_ts=1_000,
        decision_id="d1",
        client_order_id="c1",
        venue_order_id="v1",
        side="buy",
        fill_price=0.60,
        fill_size=1.0,
        fill_ts=1_000,
    )
    RealFillReconciliation(
        sequence_no=2,
        event_type="RealFillReconciliation",
        source_ts=None,
        recv_ts=1_100,
        decision_id="d1",
        venue_order_key="vk1",
        reconciled_state="RESOLVED",
        reconciled_fill_size=1.0,
    )


def test_score_run_untouched_after_dust_execution_session():
    run = _representative_run_e1t6()
    before = score_run(run)

    _simulate_r4a_dust_execution_session()

    after = score_run(run)
    # AC-016/SEC-004: the directional leaderboard is byte-identical across an R4-A session.
    assert_score_run_untouched(before, after)  # raises iff before != after

    # The sealed maker arena result on disk is byte-identical to its committed content.
    sealed = _REPO_ROOT / _SEALED_MAKER_RESULT
    on_disk = sealed.read_bytes()
    committed = subprocess.run(
        ["git", "show", f"HEAD:{_SEALED_MAKER_RESULT}"],
        cwd=_REPO_ROOT, capture_output=True, check=True,
    ).stdout
    assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(committed).hexdigest(), (
        "sealed maker-arena-result.json must be byte-identical to its committed content"
    )
