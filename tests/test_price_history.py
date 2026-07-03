import pytest

from veridex.venues.price_history import (
    VenuePriceHistoryFrame,
    VenuePriceHistoryPack,
    compute_price_history_hash,
    fetch_price_history,
)
from veridex.venues.polymarket import native_to_decimal
from veridex.venues.polymarket_resolver import ResolvedMarket


def test_from_native_sets_decimal_via_native_to_decimal_not_the_raw_q():
    f = VenuePriceHistoryFrame.from_native(
        ts=1000,
        fixture_id=17952170,
        market_ref="1X2|home|full",
        condition_id="0xabc",
        token_id="tok1",
        native_price=0.62,
        price_kind="clob-prices-history",
        fidelity_s=60,
    )
    assert f.native_price == 0.62
    assert f.venue_decimal_price == native_to_decimal(0.62)
    assert f.venue_decimal_price != 0.62
    assert f.provenance == "backfilled-price-history"


def test_price_history_hash_is_injective_and_changes_with_content(tmp_path):
    (tmp_path / "frames.jsonl").write_text('{"ts": 1, "p": 0.5}\n')
    h1 = compute_price_history_hash(tmp_path, "frames.jsonl")
    h2 = compute_price_history_hash(tmp_path, "frames.jsonl")
    assert h1 == h2  # deterministic

    (tmp_path / "frames.jsonl").write_text('{"ts": 2, "p": 0.6}\n')
    h3 = compute_price_history_hash(tmp_path, "frames.jsonl")
    assert h3 != h1  # content change is detected

    # Length-prefixing makes (name, bytes) provably injective: a differently-split
    # filename/content pair that would collide under naive concatenation must not collide here.
    (tmp_path / "ab").write_text("c")
    (tmp_path / "a").write_text("bc")
    h_ab = compute_price_history_hash(tmp_path, "ab")
    h_a = compute_price_history_hash(tmp_path, "a")
    assert h_ab != h_a


def test_pack_carries_artifact_content_hash_field(tmp_path):
    (tmp_path / "frames.jsonl").write_text('{"ts": 1, "p": 0.5}\n')
    content_hash = compute_price_history_hash(tmp_path, "frames.jsonl")

    p = VenuePriceHistoryPack(
        fixture_id=17952170,
        frames_file="frames.jsonl",
        artifact_content_hash=content_hash,
    )

    assert p.pack_version == 1
    assert p.artifact_content_hash == content_hash
    assert p.provenance == "backfilled-price-history"
    assert not hasattr(p, "evidence_hash")


class _FakeClient:
    """No-network fake — records the requested token_id and returns fixed points."""

    def __init__(self, points: list[dict]) -> None:
        self.points = points
        self.requested_token_id: str | None = None

    async def get_prices_history(self, token_id: str) -> list[dict]:
        self.requested_token_id = token_id
        return self.points


async def test_fetch_price_history_converts_every_point_to_decimal():
    resolved = ResolvedMarket(
        condition_id="0xabc",
        token_id_yes="tok-yes",
        token_id_no="tok-no",
        tick_size=0.01,
    )
    client = _FakeClient([{"t": 1000, "p": 0.62}, {"t": 1060, "p": 0.62}])

    frames = await fetch_price_history(
        resolved,
        "home",
        fixture_id=17952170,
        market_ref="1X2|home|full",
        fidelity_s=60,
        client=client,
    )

    assert client.requested_token_id == "tok-yes"
    assert len(frames) == 2
    for frame in frames:
        assert frame.native_price == 0.62
        assert frame.venue_decimal_price == pytest.approx(native_to_decimal(0.62))
        assert frame.provenance == "backfilled-price-history"
