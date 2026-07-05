"""E6 pinned self-verifying runner tests (PAT-001 / CON-012 / AC-001/007/008).

Covers ``scripts/txline_live/run_event_fork_probe.py``:

* ``run_probe`` VOIDs on config-hash drift BEFORE any pack/scores I/O (the
  Run-002 precedent -- fail closed before touching the data).
* An end-to-end pass over the real pinned fixture universe returns the sealed
  result dict (config hash + verdict + per-event audit + tallies) and writes NO
  file when ``seal=False`` (the write path is operator-gated, never in tests).
* A CUSTOM direct-imports-only AST audit proves the ``event_probe`` package and
  the runner import nothing from the trust core (``veridex.law``,
  ``veridex.scoring``, ``veridex.verifier``, ``veridex.checks``,
  ``veridex.runtime.evidence``) -- while allowing ``veridex.ingest`` (the probe
  needs it) and ``veridex.strategies.market_quality`` (CON-016 band).
* The read-only ReplayPacks are never mutated by a run.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

from scripts.txline_live import run_event_fork_probe as runner
from veridex.backtest.event_probe.config import ProbeConfig, ProbeVoidError

#: veridex-arena repo root (tests/event_probe/test_runner.py -> parents[2]).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVENT_PROBE_DIR = _REPO_ROOT / "veridex" / "backtest" / "event_probe"
_RUNNER_FILE = _REPO_ROOT / "scripts" / "txline_live" / "run_event_fork_probe.py"

#: The trust-core modules a rung-1 probe (CON-012) must NOT import DIRECTLY.
#: ``veridex.ingest`` and ``veridex.strategies.market_quality`` are ALLOWED.
_FORBIDDEN_TRUST_MODULES = frozenset(
    {
        "veridex.law",
        "veridex.scoring",
        "veridex.verifier",
        "veridex.checks",
        "veridex.runtime.evidence",
    }
)


def _no_write_open(real_open):
    """Wrap ``builtins.open`` to fail loudly on any write-mode access."""

    def _guard(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"run_probe wrote a file: open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    return _guard


def _imported_modules(pyfile: Path) -> list[str]:
    """All absolute module targets imported by ``pyfile`` (direct imports only).

    Walks ``ast.Import`` / ``ast.ImportFrom`` (``level == 0`` only -- relative
    imports are local package refs) and returns every fully-qualified module a
    static reader would resolve: ``import a.b`` -> ``a.b``; ``from a.b import c``
    -> both ``a.b`` and the composed ``a.b.c`` (so ``from veridex.runtime import
    evidence`` is caught via the composed form).
    """
    tree = ast.parse(pyfile.read_text(), filename=str(pyfile))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _is_forbidden(module: str) -> bool:
    """True iff ``module`` (or a dotted prefix) is a forbidden trust-core module.

    Prefix-matching catches ``veridex.runtime.evidence.foo`` via
    ``veridex.runtime.evidence`` while leaving siblings like
    ``veridex.runtime.window`` and ``veridex.strategies.market_quality`` alone.
    """
    parts = module.split(".")
    return any(
        ".".join(parts[:i]) in _FORBIDDEN_TRUST_MODULES for i in range(1, len(parts) + 1)
    )


def _pack_tree_snapshot() -> dict[str, tuple[int, int]]:
    """Map each pinned pack file to ``(size, mtime_ns)`` -- a mutation fingerprint."""
    snapshot: dict[str, tuple[int, int]] = {}
    for fid in runner.PINNED_FIXTURES:
        pack_dir = runner.PACKS_DIR / str(fid)
        for path in sorted(pack_dir.iterdir()):
            if path.is_file():
                st = path.stat()
                snapshot[str(path)] = (st.st_size, st.st_mtime_ns)
    return snapshot


def test_verify_pinned_runs_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drifted config VOIDs BEFORE any pack load (PAT-001 / AC-007)."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("I/O happened: load_pack_marketstates called before VOID")

    monkeypatch.setattr(runner, "load_pack_marketstates", _boom)

    # A config whose hash != EXPECTED_CONFIG_HASH (any drifted threshold).
    drifted = ProbeConfig(seed=1)
    assert drifted.config_hash() != runner.EXPECTED_CONFIG_HASH

    with pytest.raises(ProbeVoidError):
        runner.run_probe(drifted, seal=False)


def test_end_to_end_returns_sealed_dict_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real pinned-universe run returns the sealed dict and writes NO file."""
    monkeypatch.setattr(builtins, "open", _no_write_open(builtins.open))

    existed_before = runner.RESULT_PATH.exists()
    result = runner.run_probe(seal=False)

    assert isinstance(result, dict)
    assert result["config_hash"] == runner.EXPECTED_CONFIG_HASH
    assert "overall_verdict" in result
    assert isinstance(result["event_records"], list)
    assert "class_counts" in result
    assert "excluded_by_reason" in result
    # The pinned universe carries real goal events -> a populated audit trail.
    assert len(result["event_records"]) > 0
    # The runner carries ALL FIVE CON-007 slice dimensions per event (AC-008),
    # each a named non-empty bucket -- not just scoring_side.
    _slice_keys = {
        "scoring_side", "favorite_status", "score_context", "half", "match_timing",
    }
    for row in result["event_records"]:
        assert set(row["slice_tags"]) == _slice_keys
        assert all(isinstance(v, str) and v for v in row["slice_tags"].values())

    # §4 top-level conformance fields are present.
    assert result["fixtures"] == list(runner.PINNED_FIXTURES)
    assert result["total_goal_events"] == len(result["event_records"])
    assert result["eligible_events"] == result["global"]["n"]
    assert result["verdict"] == result["overall_verdict"]
    assert "CON-014" in result["predeclared_defaults_note"]
    assert "raw_delta_imm_median" in result
    assert "raw_delta_settle_median" in result
    # Extraction excludes are merged into excluded_by_reason (keys always present,
    # even at zero -- extract_goal_events initializes all three reasons).
    for reason in ("decreasing_score", "ambiguous_delta", "unparseable"):
        assert reason in result["excluded_by_reason"]

    # seal=False writes nothing: the operator-gated result JSON is not created.
    assert runner.RESULT_PATH.exists() == existed_before


def test_import_boundary_direct() -> None:
    """Custom AST audit: no event_probe/* or runner file DIRECTLY imports a
    trust-core module; ``veridex.ingest`` / ``market_quality`` remain allowed."""
    pyfiles = sorted(_EVENT_PROBE_DIR.glob("*.py")) + [_RUNNER_FILE]
    assert _RUNNER_FILE.exists()
    for pyfile in pyfiles:
        for module in _imported_modules(pyfile):
            assert not _is_forbidden(module), (
                f"{pyfile.relative_to(_REPO_ROOT)} directly imports forbidden "
                f"trust-core module {module!r}"
            )
    # Guard the audit itself: ingest + market_quality must NOT be flagged.
    assert not _is_forbidden("veridex.ingest.replay_pack")
    assert not _is_forbidden("veridex.strategies.market_quality")
    # ...and a genuine trust import MUST be flagged.
    assert _is_forbidden("veridex.runtime.evidence")


def test_packs_not_mutated() -> None:
    """A run reads the ReplayPacks read-only -- byte sizes + mtimes unchanged."""
    before = _pack_tree_snapshot()
    runner.run_probe(seal=False)
    after = _pack_tree_snapshot()
    assert before == after


def test_default_run_writes_no_result_json() -> None:
    """The default (``seal=False``) run never creates the sealed result JSON."""
    existed_before = runner.RESULT_PATH.exists()
    runner.run_probe(seal=False)
    assert runner.RESULT_PATH.exists() == existed_before
