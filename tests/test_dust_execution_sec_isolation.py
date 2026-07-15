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


# =====================================================================================
# E3-T3 keep-green (REQ-016): the distinct R4-A resting-order lane must NOT touch the sealed
# directional taker ``Order`` contract. Its ``tif`` Literal still bans GTC — asserted from within
# the dust isolation suite so the "R4-A introduced a separate resting type, not an overload"
# invariant is guarded here too (mirrors test_venue_adapter_v2::test_order_tif_gtc_is_unrepresentable).
# =====================================================================================
from pydantic import ValidationError as _ValidationError  # noqa: E402

from veridex.venues.base import Order as _TakerOrderE3T3  # noqa: E402


def test_taker_order_still_bans_gtc_keep_green() -> None:
    with pytest.raises(_ValidationError):
        _TakerOrderE3T3(
            market_ref="OU|2.5|full", side="over", size=100.0, price=2.0,
            venue="polymarket", client_order_id="c1", tif="GTC",  # type: ignore[arg-type]
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


# =====================================================================================
# E3-T7 (SEC / REQ-018, §6 group 19): the NO-LOCAL-KEY AST bar — the WHOLE Mode-B dust
# lane (the compile→sign→post process, Fable-m4) must import NO local-key crypto library.
# The control plane can custody-sign ONLY through the injected Privy client (recording-fake
# in tests); a local-key constructor anywhere in the lane would let it sign with a local key,
# defeating the entire money-network boundary. This AST bar closes over EVERY dust_execution
# module (not one file) and is teeth-checked with a positive control.
# =====================================================================================
import importlib as _importlib  # noqa: E402
import inspect as _inspect  # noqa: E402

# Local-key crypto surfaces forbidden anywhere in the Mode-B lane: module paths + signer symbols.
_NO_LOCAL_KEY_BANNED = {"eth_account", "eth_keys", "coincurve", "web3", "UtilsSigner", "Account"}


def _module_imported_surface(modname: str) -> set[str]:
    """Every imported module path AND imported/alias symbol name in ``modname`` (AST — code only).

    Inspects only ``import`` / ``from ... import`` nodes, so a prose mention of ``eth_account`` in a
    docstring or comment (the lane files carry such disclaimers) can never false-trip the bar.
    """
    mod = _importlib.import_module(modname)
    tree = ast.parse(_inspect.getsource(mod))
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


def test_no_local_key_crypto_anywhere_in_dust_execution_lane() -> None:
    # (1) GREEN on the real lane: no dust_execution module imports a local-key crypto library.
    modnames = _walk_modnames(veridex.dust_execution, "veridex.dust_execution.")
    # The load-bearing Mode-B modules must actually be in the scanned set (anti-inert).
    for required in (
        "veridex.dust_execution.privy_control_plane",
        "veridex.dust_execution.wallet_binding",
        "veridex.dust_execution.keyless_read_client",
        "veridex.dust_execution.signing_compiler",
        "veridex.dust_execution.order_commitment",
    ):
        assert required in modnames, f"Mode-B module set is missing {required}"
    offenders = {
        m: sorted(_module_imported_surface(m) & _NO_LOCAL_KEY_BANNED)
        for m in modnames
        if _module_imported_surface(m) & _NO_LOCAL_KEY_BANNED
    }
    assert not offenders, f"dust_execution lane must import NO local-key crypto library: {offenders}"

    # (2) Positive control (teeth): a synthetic module that REALLY imports a banned surface IS caught
    # by the same AST detector — a bar that can catch nothing is worthless.
    surface = _NO_LOCAL_KEY_BANNED
    fake_source = "from eth_account import Account\nimport web3\nVALUE = 1\n"
    fake_tree = ast.parse(fake_source)
    caught: set[str] = set()
    for node in ast.walk(fake_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                caught.add(alias.name)
                caught.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                caught.add(node.module)
            for alias in node.names:
                caught.add(alias.name)
    assert caught & surface == {"eth_account", "Account", "web3"}, (
        "no-local-key detector must flag banned local-key surfaces used as real imports"
    )


# =====================================================================================
# E7-T2 (SEC-006, AC-020/022, §6 group 10): the AGENT-BOUNDARY isolation audit — the two
# properties proving the UNTRUSTED R4-B/agent surface can neither REACH money-network
# capability nor MOVE admission.
#
# (1) STRUCTURAL import-audit (mirrors test_no_r3_r4_code.py's AST/import-walk technique): the
#     agent-facing tool surface — the facade BOUNDARY module (the typed R4-B intent -> R4-A
#     contracts + the injectable proposer) and the ``tools=[]`` decision agent — imports NONE of:
#     the raw venue writes ``submit_order`` / ``cancel_order`` / ``cancel_all_orders``, a
#     wire-capable / key-export signer surface (``SignerBackedWriteSeam`` + local-key crypto), or
#     the vendored CLOB client (``veridex.venues._vendor.polymarket_clob``).
#
#     SCOPE — honest limitation (the E3-era lesson: the AST bar is NOT transitive). This is a
#     DIRECT PER-MODULE import-surface audit: the WHOLE-AST import set of EACH named agent-facing
#     module (so it ALSO catches a lazy/deferred in-function import and a ``TYPE_CHECKING`` import)
#     — NOT a transitive closure. A transitive closure is DELIBERATELY not used here: the facade
#     delegates execution to the privileged runner via a SANCTIONED lazy ``import run_dust_execution``,
#     and following that edge would reach the runner's LEGITIMATE venue-write calls (a false
#     positive). The property proven is precisely: the agent-facing surface does not ITSELF import
#     raw write / key-export / vendored-client capability — money-network reach exists ONLY BEHIND
#     the boundary, in the operator-injected runner. Teeth: a REAL import of ``submit_order`` (or the
#     vendored CLOB client) into ``facade.py`` flips the audit RED, as does the synthetic control.
#
# (2) BEHAVIORAL (metadata cannot move admission, AC-022): the SAME admitted request state with the
#     SAME pinned hashes but DIFFERENT untrusted agent ``confidence`` / ``reason`` drives the runner
#     to an IDENTICAL admission (verdict + reason_codes + policy_hash). Distinct from E6-T4's wire
#     SIZE binding — this is the admission VERDICT. Teeth: making the admission path read
#     ``confidence`` flips the identical-admission assertion RED.
# =====================================================================================
# Reuse the REAL facade drivers (same pattern as ``from tests.test_no_r3_r4_code import ...``) so the
# behavioral half exercises the identical admission path, not a re-implementation.
from tests.test_dust_execution_facade import (  # noqa: E402
    _drive_facade,
    _mm_env,
    _mm_manifest,
)
from veridex.dust_execution.facade import MMExecutionToolRequest, MMIntentParams  # noqa: E402
from veridex.dust_execution.signer import LocalFakeWalletControlPlane  # noqa: E402
from veridex.venues.sx_bet import FakeVenueAdapter  # noqa: E402

# The agent-facing tool surface audited: the facade BOUNDARY module (the typed R4-B intent -> R4-A
# contracts + the injectable proposer), the ``tools=[]`` decision agent, and the two R4-B strategy
# surfaces an agent's intent path DIRECTLY touches — the ``execution_adapter`` (typed R4-B intent ->
# high-level facade request; SEC-003) and the ``assembler`` (quote assembly from observations;
# SEC-003). Everything an agent / the R4-B intent path DIRECTLY touches lives here; the money-network
# runner is imported LAZILY behind the boundary and is intentionally NOT in this set (see the SCOPE
# note above). ``veridex.runtime`` (the privileged runner) is deliberately NOT listed — it is created
# by E7-T4 and the audit ``import_module``s each name, so listing it early would ModuleNotFoundError.
_AGENT_FACING_MODULES = (
    "veridex.dust_execution.facade",
    "veridex.runtime.agent",
    "veridex.mm_strategy.execution_adapter",
    "veridex.mm_strategy.assembler",
)

# Forbidden as EXACT imported symbol names: raw venue write methods + a wire-capable/key-export signer
# surface + local-key crypto (reuses the E3-T7 no-local-key set for the crypto surfaces).
_AGENT_BOUNDARY_FORBIDDEN_NAMES = frozenset(
    {"submit_order", "cancel_order", "cancel_all_orders", "SignerBackedWriteSeam"}
) | _NO_LOCAL_KEY_BANNED

# Forbidden as a case-insensitive SUBSTRING of any imported module PATH: the vendored CLOB client.
_AGENT_BOUNDARY_FORBIDDEN_PATH_FRAGMENTS = ("_vendor", "polymarket_clob")


def _imported_surface_from_source(source: str) -> set[str]:
    """Every imported module path + top segment + imported/alias symbol name in ``source`` (AST).

    Code-only (``import`` / ``from ... import`` nodes), so a prose mention in a docstring/comment can
    never trip the audit. Walks the WHOLE tree, so a lazy in-function import and a ``TYPE_CHECKING``
    import are both present in the returned surface. Mirrors ``_module_imported_surface`` above but
    takes a source string, so the same detector covers real modules AND the synthetic controls.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
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


def _agent_boundary_import_offenders(surface: set[str]) -> list[str]:
    """Forbidden money-network surfaces present in an imported ``surface``.

    Flags an EXACT raw-write / wire-signer / local-key crypto NAME, OR any import PATH carrying a
    vendored-CLOB-client fragment. Sorted for a readable assert.
    """
    offenders = set(surface) & _AGENT_BOUNDARY_FORBIDDEN_NAMES
    for name in surface:
        low = name.lower()
        if any(fragment in low for fragment in _AGENT_BOUNDARY_FORBIDDEN_PATH_FRAGMENTS):
            offenders.add(name)
    return sorted(offenders)


def test_agent_facing_surface_imports_no_raw_write_signer_or_vendored_clob() -> None:
    # (1) GREEN on the real agent-facing surface: neither the facade boundary module nor the
    # tools=[] decision agent imports a raw venue write, a wire-capable/key-export signer, or the
    # vendored CLOB client. Whole-AST PER MODULE (direct, not transitive — see the SCOPE note): a
    # lazy/deferred or TYPE_CHECKING import is therefore included in each module's surface.
    scanned: dict[str, set[str]] = {}
    for modname in _AGENT_FACING_MODULES:
        mod = _importlib.import_module(modname)
        surface = _imported_surface_from_source(_inspect.getsource(mod))
        assert surface, f"agent-facing module {modname} yielded an empty import surface (anti-inert)"
        scanned[modname] = surface

    offenders = {
        m: _agent_boundary_import_offenders(s)
        for m, s in scanned.items()
        if _agent_boundary_import_offenders(s)
    }
    assert not offenders, (
        "agent-facing tool surface must not import raw venue writes / a wire-capable signer / the "
        f"vendored CLOB client (money-network reach lives ONLY behind the boundary): {offenders}"
    )

    # Anti-inert: the facade's LEGITIMATE type-only ``VenueAdapter`` / ``Signer`` imports (under
    # ``if TYPE_CHECKING``) ARE in the scanned surface — proving the whole-AST walk really sees those
    # imports — yet they are correctly NOT flagged, because the bar keys on the raw WRITE / key-export
    # names, never on the provider-neutral Protocol TYPES the boundary is built from.
    facade_surface = scanned["veridex.dust_execution.facade"]
    assert "VenueAdapter" in facade_surface, "whole-AST walk must see the TYPE_CHECKING VenueAdapter"
    assert "Signer" in facade_surface, "whole-AST walk must see the TYPE_CHECKING Signer type import"

    # (2) Positive control (teeth): a synthetic agent-facing module that REALLY imports a raw
    # submit_order/cancel_order, the vendored CLOB client, a wire-capable signer seam, and a local-key
    # signer IS caught by the SAME detector — a bar that can catch nothing is worthless.
    leak_src = (
        "from veridex.venues.base import submit_order, cancel_order\n"
        "from veridex.venues._vendor.polymarket_clob.client import Polymarket\n"
        "from veridex.dust_execution.signer import SignerBackedWriteSeam\n"
        "from eth_account import Account\n"
    )
    caught = _agent_boundary_import_offenders(_imported_surface_from_source(leak_src))
    assert {"submit_order", "cancel_order", "SignerBackedWriteSeam", "Account"} <= set(caught), (
        f"detector must flag raw writes + a wire signer + local-key crypto used as real imports: {caught}"
    )
    assert any(
        fragment in c.lower() for c in caught for fragment in _AGENT_BOUNDARY_FORBIDDEN_PATH_FRAGMENTS
    ), "the vendored CLOB client import path must be caught by a forbidden-fragment match"

    # (3) Negative control (no prose false-positive): the forbidden surfaces named ONLY inside a
    # docstring/comment are invisible to the AST detector (mirrors the real lane's prose disclaimers).
    prose_src = (
        '"""submit_order / cancel_order / the _vendor polymarket_clob client / eth_account /\n'
        'SignerBackedWriteSeam are reachable ONLY behind the boundary — never imported here."""\n'
        "# submit_order cancel_order _vendor polymarket_clob eth_account SignerBackedWriteSeam\n"
        "SAFE = 1\n"
    )
    assert not _agent_boundary_import_offenders(_imported_surface_from_source(prose_src)), (
        "the import audit must ignore forbidden surfaces named only in prose (docstring/comment)"
    )


def _admitted_request_with_metadata(
    manifest: object, envelope: object, *, reason: str, confidence: float
) -> MMExecutionToolRequest:
    """A sanctioned, hash-matched agent intent carrying UNTRUSTED ``reason`` / ``confidence`` metadata.

    Identical pinned hashes + identical intent for every call; only the untrusted metadata varies — so
    two such requests differ ONLY in the fields the boundary must ignore.
    """
    return MMExecutionToolRequest.build(
        intent_kind="make_quote",
        intent_params=MMIntentParams(
            token_id="0xtokenYES", side="BUY", price=0.49, size=1.0, tif="GTC", client_order_id="coid-1"
        ),
        strategy_id=manifest.strategy_id,  # type: ignore[attr-defined]
        strategy_config_hash=manifest.strategy_config_hash,  # type: ignore[attr-defined]
        policy_hash=envelope.policy_hash(),  # type: ignore[attr-defined]
        session_id="sess-mm-sec006",
        manifest_hash=manifest.manifest_hash(),  # type: ignore[attr-defined]
        evidence_class="EXPERIMENTAL_DUST",
        mode="dry_run",
        admitted_manifest_hash=manifest.manifest_hash(),  # type: ignore[attr-defined]
        admitted_policy_hash=envelope.policy_hash(),  # type: ignore[attr-defined]
        admitted_strategy_config_hash=manifest.strategy_config_hash,  # type: ignore[attr-defined]
        reason=reason,
        confidence=confidence,
    )


async def test_agent_metadata_cannot_move_admission_identical_verdict_reason_codes_policy_hash() -> None:
    # SEC-006/AC-022 behavioral half: identical request state + identical pinned hashes but DIFFERENT
    # untrusted agent confidence/reason drive the runner to an IDENTICAL admission (verdict +
    # reason_codes + policy_hash). The agent metadata is UX narration with NO gate effect; it can
    # never move the deterministic admission. Distinct from E6-T4's wire-SIZE binding — this is the
    # admission VERDICT. Teeth: making the admission path read confidence flips this RED.
    manifest = _mm_manifest()
    envelope = _mm_env()

    low = _admitted_request_with_metadata(
        manifest, envelope, reason="looks marginal, keep it tiny", confidence=0.01
    )
    high = _admitted_request_with_metadata(
        manifest, envelope, reason="SURE THING — size this up now", confidence=0.99
    )

    # Non-vacuity: the two requests differ in the UNTRUSTED metadata yet share every pinned hash.
    assert low.confidence != high.confidence and low.reason != high.reason
    assert (low.manifest_hash, low.policy_hash, low.strategy_config_hash) == (
        high.manifest_hash,
        high.policy_hash,
        high.strategy_config_hash,
    )

    res_low = await _drive_facade(
        manifest, envelope, low,
        adapter=FakeVenueAdapter(fill=True), signer=LocalFakeWalletControlPlane(), sink=None,
    )
    res_high = await _drive_facade(
        manifest, envelope, high,
        adapter=FakeVenueAdapter(fill=True), signer=LocalFakeWalletControlPlane(), sink=None,
    )

    # IDENTICAL admission across the metadata delta: verdict + reason_codes + policy_hash.
    assert res_low.admission == res_high.admission == "APPROVED"
    assert res_low.reason_codes == res_high.reason_codes
    assert res_low.policy_hash == res_high.policy_hash


def test_agent_request_cannot_even_carry_an_analysis_field() -> None:
    # Structural corollary (AC-022): ``analysis`` is not a modelled field, so an agent cannot even
    # SUPPLY free-form analysis across the boundary — extra="forbid" rejects it at construction.
    # ``reason`` / ``confidence`` are the ONLY untrusted metadata, and (above) they cannot move
    # admission — so no agent narration of any spelling reaches a policy/law/rank/execution outcome.
    manifest = _mm_manifest()
    envelope = _mm_env()
    base: dict[str, object] = {
        "intent_kind": "make_quote",
        "intent_params": MMIntentParams(
            token_id="0xtokenYES", side="BUY", price=0.49, size=1.0, tif="GTC", client_order_id="coid-1"
        ),
        "strategy_id": manifest.strategy_id,
        "strategy_config_hash": manifest.strategy_config_hash,
        "policy_hash": envelope.policy_hash(),
        "session_id": "sess-mm-sec006",
        "manifest_hash": manifest.manifest_hash(),
        "evidence_class": "EXPERIMENTAL_DUST",
        "mode": "dry_run",
    }
    # A fully-modelled construction succeeds (the fields ARE the boundary's typed surface)...
    MMExecutionToolRequest(**base)  # type: ignore[arg-type]
    # ...but an unmodelled ``analysis`` field is rejected — the boundary cannot carry it at all.
    with pytest.raises(_ValidationError):
        MMExecutionToolRequest(**{**base, "analysis": "the agent's private thesis"})  # type: ignore[arg-type]


# =====================================================================================
# E7-T5 (SEC-001, CON-002/003, REQ-011/014/015, AC-014/019/025/030, §6 group 13): the
# diagnostic post-trade markout module + the mandatory honesty labels + the scoped-negative
# relabel-proof. Three LOAD-BEARING isolation/honesty properties proven here:
#
#   (1) DIAGNOSTIC markout, NON-RANKABLE + NON-MUTATING. ``analysis.compute_markout`` derives a
#       NEW ``PostTradeMarkoutEvent`` keyed by ``decision_id`` from a SEALED own-fill event WITHOUT
#       mutating that sealed event (byte-identity proven before/after). Its output field names
#       (``reference_price`` / ``markout_bps``) are already on the SEC-006 rank denylist (E1-T5),
#       so the module can NEVER emit a markout that reaches scoring / leaderboard / maker-leaderboard.
#       RED (module absent): the derive helper does not exist → ImportError.
#   (2) MANDATORY honesty labels (AC-025). Every dust run MUST carry ``DUST_LIVE`` / the evidence
#       class / ``UNCALIBRATED`` / ``NOT_PROVEN_EDGE``. The pinned Literals make a wrong label
#       UNCONSTRUCTABLE; ``assert_mandatory_dust_run_labels`` additionally rejects any label-like
#       object that downgrades a mandatory value.
#   (3) SCOPED-NEGATIVE not relabellable (SEC-001/CON-003). A dust run that proved SAFETY, not alpha,
#       is a scoped-negative finding; its evidence class is STRUCTURALLY pinned to ``EXPERIMENTAL_DUST``
#       (there is no channel to set it) and an EXPLICIT promotion request (``EVIDENCE_GATED`` /
#       ``PROMOTED``) from ANY request field / label / LLM output / metadata is REFUSED fail-closed.
#       R4-A never claims proven alpha; only Gate B (out of R4-A scope) controls promotion.
# =====================================================================================
from veridex.dust_execution.analysis import (  # noqa: E402
    REQUIRED_DUST_RUN_LABELS,
    SCOPED_NEGATIVE_EVIDENCE_CLASS,
    ScopedNegativeRelabelError,
    assert_mandatory_dust_run_labels,
    compute_markout,
    label_for_scoped_negative,
    reject_scoped_negative_relabel,
)
from veridex.dust_execution.contracts import DustRunLabelEvent as _DustRunLabelEvent  # noqa: E402
from veridex.dust_execution.contracts import OwnFillEvent as _OwnFillE7  # noqa: E402


def _sealed_own_fill_e7t5() -> _OwnFillE7:
    """A SEALED (frozen) realized own-fill event — the source a diagnostic markout is derived from."""
    return _OwnFillE7(
        sequence_no=7, event_type="OwnFillEvent", source_ts=None, recv_ts=5_000,
        decision_id="dec-e7t5", client_order_id="coid-e7t5", venue_order_id="v-e7t5",
        side="BUY", fill_price=0.40, fill_size=1.0, fill_ts=5_000,
    )


# --- Property (1): diagnostic markout is keyed by decision_id, non-mutating, non-rankable ---


def test_compute_markout_is_keyed_by_decision_id() -> None:
    fill = _sealed_own_fill_e7t5()
    markout = compute_markout(
        fill, reference_price=0.55, horizon_ms=1_000, sequence_no=8, recv_ts=6_000
    )
    # The diagnostic record is keyed by the SAME decision_id as the sealed fill it was derived from.
    assert markout.decision_id == fill.decision_id == "dec-e7t5"
    # BUY filled at 0.40, reference rose to 0.55 → +0.15 favorable = +1500 bps (native-prob move).
    assert markout.markout_bps == pytest.approx(1500.0)
    assert markout.reference_price == 0.55
    assert markout.horizon_ms == 1_000


def test_compute_markout_does_not_mutate_the_sealed_fill_event() -> None:
    # NON-MUTATION (AC-014): deriving markout reads the sealed fill; it must never write back. Prove
    # byte-identity of the source event's canonical dump + evidence hash before/after, and that the
    # returned record is a DISTINCT object (a NEW diagnostic, not the mutated source).
    fill = _sealed_own_fill_e7t5()
    dump_before = fill.model_dump()
    hash_before = fill.config_hash()

    markout = compute_markout(
        fill, reference_price=0.55, horizon_ms=1_000, sequence_no=8, recv_ts=6_000
    )

    assert fill.model_dump() == dump_before, "sealed fill event must be byte-identical after markout"
    assert fill.config_hash() == hash_before, "sealed fill evidence hash must be unchanged"
    assert markout is not fill  # a NEW diagnostic record, never the mutated source


def test_compute_markout_output_rejected_by_all_three_rank_surfaces() -> None:
    # NON-RANKABLE (AC-014): the ACTUAL analysis.py output dump, merged into a complete valid rank
    # row, is rejected by all three rank surfaces (incl. the raw sorted(..., key=...) bypass). Proves
    # the NEW module emits ONLY already-denylisted outcome fields — it can never invent a rankable one.
    fill = _sealed_own_fill_e7t5()
    markout = compute_markout(
        fill, reference_price=0.55, horizon_ms=1_000, sequence_no=8, recv_ts=6_000
    )
    dump = markout.model_dump()
    for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
        row = {**base, **dump}
        with pytest.raises(AssertionError):
            keyfn(row)
        with pytest.raises(AssertionError):
            sorted([row], key=keyfn)


def test_compute_markout_signs_a_sell_the_opposite_way() -> None:
    # A SELL is favorable when the reference FALLS below the fill; the sign flips vs a BUY.
    sell = _OwnFillE7(
        sequence_no=7, event_type="OwnFillEvent", source_ts=None, recv_ts=5_000,
        decision_id="dec-sell", client_order_id="coid", venue_order_id="v", side="SELL",
        fill_price=0.60, fill_size=1.0, fill_ts=5_000,
    )
    markout = compute_markout(sell, reference_price=0.45, horizon_ms=500, sequence_no=8, recv_ts=6_000)
    # SELL at 0.60, reference fell to 0.45 → +0.15 favorable = +1500 bps.
    assert markout.markout_bps == pytest.approx(1500.0)


# --- Property (2): mandatory honesty labels ---


def test_dust_run_label_run_label_is_mandatory_pinned() -> None:
    # A run cannot emit a label with a softened ``run_label`` — the pinned Literal rejects it.
    with pytest.raises(_ValidationError):
        _DustRunLabelEvent(
            sequence_no=1, event_type="DustRunLabelEvent", source_ts=None, recv_ts=1,
            run_label="DUST_PAPER", evidence_class="EXPERIMENTAL_DUST",  # type: ignore[arg-type]
            calibration_label="UNCALIBRATED", edge_label="NOT_PROVEN_EDGE",
        )


def test_assert_mandatory_dust_run_labels_accepts_the_full_honest_label() -> None:
    label = label_for_scoped_negative(sequence_no=9, recv_ts=1_000)
    assert_mandatory_dust_run_labels(label)  # does not raise
    assert REQUIRED_DUST_RUN_LABELS == {
        "run_label": "DUST_LIVE",
        "calibration_label": "UNCALIBRATED",
        "edge_label": "NOT_PROVEN_EDGE",
    }


@pytest.mark.parametrize(
    "field,downgrade",
    [
        ("run_label", "DUST_PAPER"),
        ("calibration_label", "CALIBRATED"),
        ("edge_label", "PROVEN_EDGE"),
    ],
)
def test_assert_mandatory_dust_run_labels_rejects_a_downgraded_value(field: str, downgrade: str) -> None:
    from types import SimpleNamespace

    values = {
        "run_label": "DUST_LIVE",
        "calibration_label": "UNCALIBRATED",
        "edge_label": "NOT_PROVEN_EDGE",
        "evidence_class": "EXPERIMENTAL_DUST",
    }
    values[field] = downgrade
    with pytest.raises(AssertionError):
        assert_mandatory_dust_run_labels(SimpleNamespace(**values))


# --- Property (3): a scoped-negative finding cannot be relabelled EVIDENCE_GATED / PROMOTED ---


def test_scoped_negative_label_is_structurally_experimental_dust() -> None:
    # No channel to set the evidence class — a scoped-negative run is INTRINSICALLY EXPERIMENTAL_DUST.
    label = label_for_scoped_negative(sequence_no=1, recv_ts=1_000)
    assert isinstance(label, _DustRunLabelEvent)
    assert label.evidence_class == "EXPERIMENTAL_DUST"
    assert SCOPED_NEGATIVE_EVIDENCE_CLASS == "EXPERIMENTAL_DUST"


@pytest.mark.parametrize(
    "hostile",
    [
        "EVIDENCE_GATED",
        "PROMOTED",
        "evidence_gated",
        "  promoted  ",
        "Promoted",
        {"evidence_class": "PROMOTED"},  # untrusted metadata
        "the model recommends we PROMOTE this to EVIDENCE_GATED",  # untrusted LLM output
    ],
)
def test_scoped_negative_relabel_request_is_refused_fail_closed(hostile: object) -> None:
    # Any request field / label input / LLM output / metadata that asks to relabel a scoped-negative
    # to a promoted evidence class is REFUSED — both via the guard and via the label emitter.
    with pytest.raises(ScopedNegativeRelabelError):
        reject_scoped_negative_relabel(hostile)
    with pytest.raises(ScopedNegativeRelabelError):
        label_for_scoped_negative(sequence_no=1, recv_ts=1_000, requested_relabel=hostile)


def test_scoped_negative_relabel_guard_is_noop_on_benign_input() -> None:
    # The guard is fail-closed on promotion ONLY — a benign/empty/experimental input is a NO-OP,
    # and the emitter still pins EXPERIMENTAL_DUST.
    reject_scoped_negative_relabel(None)
    reject_scoped_negative_relabel("EXPERIMENTAL_DUST")
    reject_scoped_negative_relabel("keep it tiny")
    label = label_for_scoped_negative(sequence_no=1, recv_ts=1_000, requested_relabel="EXPERIMENTAL_DUST")
    assert label.evidence_class == "EXPERIMENTAL_DUST"
