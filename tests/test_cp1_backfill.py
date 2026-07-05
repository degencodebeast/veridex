"""C-3 offline tests: the 1X2 prices-history backfill REUSES the M0 Frame/Pack/fetch contract.

No network in this suite (a fake client returns recorded points). These tests pin the four
C-3 invariants: (1) frames carry the AC-014 native->decimal price and the sibling Pack hashes
the WRITTEN frames file without ever growing an ``evidence_hash``; (2) the convenience wrapper
stitches ``fetch_price_history`` + JSONL write + ``compute_price_history_hash`` into one
``(frames, Pack)`` result; (3) the operator gate REFUSES to run when the C-1 coverage artifact
is absent or has zero headline-eligible fixtures (CON-001 fail-closed); (4) the live client's
query params carry the mandatory ``interval`` time component the C-1 probe learned the CLOB
``/prices-history`` endpoint 400s without.
"""

from __future__ import annotations

import json

import pytest

from scripts.txline_live.cp1_backfill import (
    CoverageGateError,
    load_headline_eligible_fixture_ids,
)
from veridex.venues.polymarket import native_to_decimal
from veridex.venues.polymarket_price_history import (
    _prices_history_params,
    _write_frames_jsonl,
    build_price_history_pack,
)
from veridex.venues.polymarket_resolver import ResolvedMarket
from veridex.venues.price_history import (
    VenuePriceHistoryPack,
    compute_price_history_hash,
    fetch_price_history,
)


class _FakeClient:
    """No-network fake mirroring ``tests/test_price_history.py``'s injection pattern."""

    def __init__(self, points: list[dict]) -> None:
        self.points = points
        self.requested_token_id: str | None = None

    async def get_prices_history(self, token_id: str) -> list[dict]:
        self.requested_token_id = token_id
        return self.points


_RESOLVED_HOME = ResolvedMarket(
    condition_id="0xabc",
    token_id_yes="tok-yes",
    token_id_no="tok-no",
    tick_size=0.01,
)


async def test_frames_are_native_to_decimal_and_pack_hashes_without_evidence(tmp_path):
    """RED (plan C-3): frames are decimal (AC-014); the Pack hashes the written file, no evidence."""
    fake_client = _FakeClient([{"t": 1000, "p": 0.62}, {"t": 1060, "p": 0.55}])
    frames = await fetch_price_history(
        _RESOLVED_HOME,
        "home",
        fixture_id=101,
        market_ref="1X2|home|full",
        fidelity_s=60,  # keyword-only after side
        client=fake_client,
    )

    assert all(f.venue_decimal_price > 1.0 for f in frames)  # from_native / AC-014

    _write_frames_jsonl(tmp_path / "frames.jsonl", frames)  # write the frames file first
    content_hash = compute_price_history_hash(tmp_path, "frames.jsonl")  # (pack_dir, frames_file)
    pack = VenuePriceHistoryPack(
        fixture_id=101, frames_file="frames.jsonl", artifact_content_hash=content_hash
    )
    assert pack.artifact_content_hash and not hasattr(pack, "evidence_hash")


async def test_build_price_history_pack_writes_jsonl_and_returns_frames_and_pack(tmp_path):
    """The convenience wrapper returns ``(frames, Pack)`` and its Pack hash matches the file bytes."""
    fake_client = _FakeClient(
        [{"t": 1000, "p": 0.62}, {"t": 1060, "p": 0.55}, {"t": 1120, "p": 0.60}]
    )
    frames, pack = await build_price_history_pack(
        _RESOLVED_HOME,
        "home",
        fixture_id=101,
        market_ref="1X2|home|full",
        fidelity_s=60,
        client=fake_client,
        pack_dir=tmp_path,
        frames_file="home.jsonl",
    )

    assert fake_client.requested_token_id == "tok-yes"  # side->token via the real resolver
    assert len(frames) == 3

    lines = (tmp_path / "home.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # one JSON object per frame
    first = json.loads(lines[0])
    assert first["native_price"] == 0.62
    assert first["venue_decimal_price"] == pytest.approx(native_to_decimal(0.62))

    # The Pack's artifact_content_hash is over the bytes actually written (not a re-hash).
    assert pack.artifact_content_hash == compute_price_history_hash(tmp_path, "home.jsonl")
    assert pack.fixture_id == 101
    assert pack.frames_file == "home.jsonl"
    assert not hasattr(pack, "evidence_hash")


def test_backfill_refuses_when_coverage_artifact_absent(tmp_path):
    """CON-001: an absent C-1 coverage artifact is a hard STOP, not a silent empty run."""
    with pytest.raises(CoverageGateError):
        load_headline_eligible_fixture_ids(tmp_path / "does-not-exist.json")


def test_backfill_refuses_when_zero_headline_eligible(tmp_path):
    """CON-001: a coverage artifact with zero headline-eligible fixtures fails closed."""
    coverage = tmp_path / "cp1-coverage.json"
    coverage.write_text(
        json.dumps(
            {"headline_eligible_fixture_ids": [], "headline_eligible_count": 0, "viable": False}
        )
    )
    with pytest.raises(CoverageGateError):
        load_headline_eligible_fixture_ids(coverage)


def test_backfill_loads_headline_eligible_ids_from_valid_artifact(tmp_path):
    """A viable coverage artifact yields exactly its headline-eligible fixture ids (the ONLY gate input)."""
    coverage = tmp_path / "cp1-coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "headline_eligible_fixture_ids": [101, 202, 303],
                "headline_eligible_count": 3,
                "viable": True,
            }
        )
    )
    assert load_headline_eligible_fixture_ids(coverage) == [101, 202, 303]


def test_prices_history_params_carry_the_mandatory_interval_time_component():
    """Integration flag (C-1): the CLOB /prices-history 400s without a time component.

    A bare ``?market=<tok>`` fails; ``interval=max`` supplies the mandatory time component so the
    real backfill pulls the full available price path instead of 400ing exactly as the probe did.
    """
    params = _prices_history_params("tok-123")
    assert params["market"] == "tok-123"
    assert params["interval"] == "max"  # the mandatory time component
    assert "fidelity" in params  # pinned point spacing
