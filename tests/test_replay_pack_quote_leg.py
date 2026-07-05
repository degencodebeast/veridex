"""M6 (S5) Task 17 — optional venue_quotes leg on a ReplayPack (content-hashed, non-evidence).

A fixture may carry an OPTIONAL ``venue_quotes`` data file alongside its ``records``. That file
joins the pack's INJECTIVE ``content_hash`` (so tampering with the recorded quotes is detected),
but venue quotes are a NON-EVIDENCE sibling: the loader marks each quote row ``evidence=False``,
and — CRITICALLY (AC-015) — the quote leg NEVER enters the sealed ``evidence_hash`` (which is
computed over the run's tick events, not the pack's quote file).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_replay_pack import _write_session
from veridex.ingest.replay_pack import _compute_content_hash, pack_from_session
from veridex.runtime.orchestrator import deterministic_agent
from veridex.runtime.window import RunWindow

_FIXTURE_ID = 5


def _real_pack(tmp_path: Path) -> Path:
    """A real, hashed pack (fixture 5) built through the recorder helpers — no quote leg yet."""
    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)
    return pack_dir


def _quote_frame(ts: int) -> dict:
    """One recorded venue quote row (decimal-odds primitives + a live-quote provenance)."""
    return {
        "ts": ts,
        "fixture_id": _FIXTURE_ID,
        "market_ref": "1X2|home",
        "best_bid_decimal": 2.0,
        "best_ask_decimal": 2.1,
        "provenance": "recorded-live-quote",
    }


def _attach_quote_leg(pack_dir: Path, fixture_id: int, frames: list[dict]) -> str:
    """Attach a ``venue_quotes`` leg to an existing pack, keeping its ``content_hash`` valid.

    Returns the (re)computed ``content_hash`` so callers can prove the leg actually changed it.
    """
    quotes_file = f"quotes_{fixture_id}.jsonl"
    (pack_dir / quotes_file).write_text("\n".join(json.dumps(f) for f in frames) + "\n")
    manifest = json.loads((pack_dir / "pack.json").read_text())
    for entry in manifest["fixtures"]:
        if entry["fixture_id"] == fixture_id:
            entry["venue_quotes"] = quotes_file
    manifest["content_hash"] = _compute_content_hash(pack_dir, manifest["fixtures"])
    (pack_dir / "pack.json").write_text(json.dumps(manifest))
    return manifest["content_hash"]


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_quote_leg",
        fixture_id=_FIXTURE_ID,
        market_allowlist=["1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


def test_venue_quotes_file_joins_content_hash_injectively(tmp_path: Path) -> None:
    """Adding a venue_quotes leg CHANGES the pack content_hash (the quote file is hashed in)."""
    pack_dir = _real_pack(tmp_path)
    fixtures = json.loads((pack_dir / "pack.json").read_text())["fixtures"]

    before = _compute_content_hash(pack_dir, fixtures)
    after = _attach_quote_leg(pack_dir, _FIXTURE_ID, [_quote_frame(100), _quote_frame(160)])

    assert before != after, "the venue_quotes file must join the injective content_hash"


def test_quote_frames_are_marked_non_evidence(tmp_path: Path) -> None:
    """The loader marks every quote row ``evidence=False`` — quotes are never sealed evidence."""
    pack_dir = _real_pack(tmp_path)
    _attach_quote_leg(pack_dir, _FIXTURE_ID, [_quote_frame(100), _quote_frame(160)])

    from veridex.ingest.replay_pack import load_pack_venue_quotes

    rows = load_pack_venue_quotes(pack_dir, _FIXTURE_ID)

    assert rows, "the quote leg should load at least one frame"
    assert all(row["evidence"] is False for row in rows)


async def test_evidence_hash_is_byte_identical_with_and_without_quote_leg(tmp_path: Path) -> None:
    """AC-015: the quote leg changes content_hash but the SEALED evidence_hash stays byte-identical.

    The evidence_hash is computed over the run's tick events; venue quotes are a content-hashed
    sibling that never reaches the seal. A returned identical evidence_hash — despite a CHANGED
    content_hash — proves the two hash scopes are disjoint (no venue-quote leak past the seal).
    """
    pack_dir = _real_pack(tmp_path)
    from veridex.backtest.runner import run_backtest  # local import keeps this test self-contained

    result_before, _ = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent()], window=_window())
    hash_before = json.loads((pack_dir / "pack.json").read_text())["content_hash"]

    hash_after = _attach_quote_leg(pack_dir, _FIXTURE_ID, [_quote_frame(100), _quote_frame(160)])
    result_after, _ = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent()], window=_window())

    # The leg genuinely changed the pack content hash (so this is not a no-op augmentation)...
    assert hash_before != hash_after
    # ...yet the sealed evidence hash is byte-identical — the quote leg never entered the seal.
    assert result_after.evidence_hash == result_before.evidence_hash
    # And no sealed run event carries a venue-quote provenance (defence in depth).
    blob = json.dumps([e.model_dump() if hasattr(e, "model_dump") else e for e in result_after.run_events])
    assert "recorded-live-quote" not in blob
    assert "backfilled-price-history" not in blob
