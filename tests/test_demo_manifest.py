"""T22 — the judge demo runner's manifest is honest and schema-valid (REQ-2D-602).

Offline + deterministic: the demo runs over the SHIPPED fixture ReplayPack (a banked, local
artifact — NO network, NO live orders) and writes a ``demo_manifest.json``. This test pins the
manifest schema ``{"runs": [{"run_id", "kind", "verify_url"}, ...], "generated_ts": <int|str>}``
and the HONESTY invariants: every ``kind`` is a truthful, non-overclaiming label; every run carries
a non-empty ``run_id`` and a ``/runs/{run_id}/verify`` URL that actually RESOLVES against the store
the demo persisted it to (a real sealed run, never a fabricated manifest row).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.demo_phase2d import (
    DEFAULT_FIXTURE_ID,
    DEFAULT_PACK_DIR,
    FLAGSHIP_STRATEGY_LABEL,
    HONEST_KINDS,
    SYNTHETIC_PROVENANCE,
    UNKNOWN_PROVENANCE,
    _pack_provenance,
    run_demo,
)
from veridex.store import InMemoryStore

_REQUIRED_RUN_KEYS = {"run_id", "kind", "verify_url"}
#: Modes that would overclaim executed / real-money activity — a proof-only demo must NEVER emit one.
_OVERCLAIMING_MODES = {"Live Guarded", "Dry Run"}


async def test_manifest_matches_schema_and_persists_resolvable_runs(tmp_path: Path) -> None:
    store = InMemoryStore()
    out_path = tmp_path / "demo_manifest.json"

    manifest = await run_demo(DEFAULT_PACK_DIR, DEFAULT_FIXTURE_ID, out_path=out_path, store=store)

    # What the script returns is byte-identical to what it wrote to disk.
    assert json.loads(out_path.read_text()) == manifest

    # --- top-level schema ---------------------------------------------------
    assert set(manifest) >= {"runs", "generated_ts"}
    assert isinstance(manifest["generated_ts"], (int, str))
    assert manifest["runs"], "manifest must contain at least one run"

    # Structured data-provenance at the top level (the shipped pack is synthetic-illustrative).
    assert manifest["data_provenance"] == SYNTHETIC_PROVENANCE
    assert manifest["synthetic_data"] is True

    # --- per-run schema + honesty + resolvability ---------------------------
    seen: set[str] = set()
    for run in manifest["runs"]:
        assert set(run) >= _REQUIRED_RUN_KEYS
        run_id = run["run_id"]
        assert isinstance(run_id, str) and run_id, "run_id must be a non-empty string"
        assert run_id not in seen, "run_ids must be unique"
        seen.add(run_id)

        assert run["kind"] in HONEST_KINDS, f"dishonest/unknown kind: {run['kind']!r}"
        assert "live" not in run["kind"].lower(), "a proof-only demo must never label a run 'live'"
        assert run.get("mode_label") not in _OVERCLAIMING_MODES

        # Provenance travels WITH every run entry — a parser reading one run dict gets the caveat.
        assert run["data_provenance"] == SYNTHETIC_PROVENANCE
        assert run["synthetic_data"] is True
        # Any run carrying a CLV number must carry its inline caveat in the SAME dict.
        if run.get("avg_clv_bps") is not None:
            caveat = run["clv_caveat"].lower()
            assert "synthetic" in caveat and "not a real" in caveat

        assert run["verify_url"] == f"/runs/{run_id}/verify"

        # The verify_url actually RESOLVES: the run was persisted to the store the demo wrote to.
        loaded = await store.load_run(run_id)
        assert loaded.run_id == run_id


async def test_flagship_backtest_is_present_and_honestly_labelled(tmp_path: Path) -> None:
    store = InMemoryStore()

    manifest = await run_demo(DEFAULT_PACK_DIR, DEFAULT_FIXTURE_ID, out_path=tmp_path / "m.json", store=store)

    kinds = {run["kind"] for run in manifest["runs"]}
    assert "backtest" in kinds, "the flagship offline story is a real backtest"

    flagship = next(run for run in manifest["runs"] if run["kind"] == "backtest")
    # The flagship is Sharp Momentum v2, labelled honestly as a Backtest (never 'Live').
    assert flagship.get("strategy_label") == FLAGSHIP_STRATEGY_LABEL == "Sharp Momentum v2"
    assert flagship.get("mode_label") == "Backtest"
    assert "Live" not in flagship.get("mode_label", "")
    # The CLV can never read as a real edge: its synthetic caveat rides in the same dict.
    assert flagship["avg_clv_bps"] is not None
    assert "synthetic" in flagship["clv_caveat"].lower()
    assert "not a real" in flagship["clv_caveat"].lower()


def _pack_with_capture(tmp_path: Path, capture: dict[str, object]) -> Path:
    """Write a minimal pack dir whose ``pack.json`` carries just the given ``capture`` block."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "pack.json").write_text(json.dumps({"capture": capture}))
    return pack_dir


def test_provenance_string_without_bool_still_reads_synthetic(tmp_path: Path) -> None:
    """Coherence: a ``provenance`` string that says synthetic — even with NO ``synthetic`` bool —
    keeps the synthetic flag + caveat (the two signals can't disagree in the caveat-off direction)."""
    pack = _pack_with_capture(tmp_path, {"provenance": SYNTHETIC_PROVENANCE})  # no synthetic bool

    provenance, is_synthetic, caveat = _pack_provenance(pack)

    assert is_synthetic is True
    assert provenance == SYNTHETIC_PROVENANCE
    assert "synthetic" in caveat.lower() and "not a real" in caveat.lower()


def test_unmarked_pack_reads_unknown_not_captured(tmp_path: Path) -> None:
    """Fail-safe: an unmarked/empty capture must NOT assert 'captured' — it reads unknown and still
    carries a cautious caveat, so an unverifiable pack never silently claims a real edge."""
    pack = _pack_with_capture(tmp_path, {})  # no synthetic, no provenance

    provenance, is_synthetic, caveat = _pack_provenance(pack)

    assert provenance == UNKNOWN_PROVENANCE
    assert provenance != "captured-odds"
    assert is_synthetic is False
    assert "unverified" in caveat.lower() and "not a claimed real edge" in caveat.lower()


def test_positively_stamped_real_pack_reads_real_not_synthetic(tmp_path: Path) -> None:
    """A producer-stamped non-synthetic provenance reads as an honest real-odds label — still a
    paper/backtest signal, never a bare live-executed edge claim."""
    pack = _pack_with_capture(tmp_path, {"provenance": "captured-txline-fixture-18172379"})

    provenance, is_synthetic, caveat = _pack_provenance(pack)

    assert is_synthetic is False
    assert provenance == "captured-txline-fixture-18172379"
    assert "synthetic" not in caveat.lower()
    assert "not a live-executed" in caveat.lower()
