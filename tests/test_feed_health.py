"""WD-4 — live-feed health + closing-line reconstruction (CON-040)."""

from __future__ import annotations

from veridex.ingest.feed_health import FeedHealthReport, feed_health, reconstruct_closing_line


def test_fresh_live_feed_is_not_stale() -> None:
    report = feed_health(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=100,
        now_ts=110,
        ticks_seen=42,
        fixture_id=18172280,
        stale_after_s=30,
    )
    assert isinstance(report, FeedHealthReport)
    assert report.staleness_s == 10
    assert report.stale is False
    assert report.connected is True


def test_old_tick_is_stale() -> None:
    report = feed_health(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=100,
        now_ts=200,
        ticks_seen=42,
        stale_after_s=30,
    )
    assert report.staleness_s == 100
    assert report.stale is True


def test_no_tick_yet_is_not_stale_but_unknown() -> None:
    report = feed_health(
        source_mode="replay",
        txline_configured=False,
        connected=False,
        last_tick_ts=None,
        now_ts=200,
        ticks_seen=0,
    )
    assert report.staleness_s is None
    assert report.stale is False


def test_reconstruct_closing_line_picks_last_pre_inrunning() -> None:
    updates = [
        {"MessageId": "a", "InRunning": False},
        {"MessageId": "b", "InRunning": False},
        {"MessageId": "c", "InRunning": True},
    ]
    closing = reconstruct_closing_line(updates)
    assert closing is not None and closing["MessageId"] == "b"


def test_reconstruct_closing_line_none_when_always_live() -> None:
    assert reconstruct_closing_line([{"MessageId": "c", "InRunning": True}]) is None
