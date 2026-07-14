"""E3-T2 tests: recorder-global mint envelope + typed per-source epoch resume (MM-R4-B).

The assembler is the SOLE author of per-source generation epochs (REQ-020b/027). It mints every
source through the ONE global :class:`~veridex.live_recorder.recorder.LiveRecorder` sequence
authority, reads the durable-max per-source generations back from the ``MintEvent`` tape rows (never
from ``meta.json``), and increments them on an assembler restart. Absent market-status rows fail
closed to ``UNKNOWN`` — the harness never synthesizes ``ACTIVE``.
"""

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import read_session
from veridex.mm_strategy.assembler import (
    latest_market_status,
    mint,
    read_durable_source_generations,
    record_market_status,
    resume_source_generations,
)
from veridex.mm_strategy.contracts import (
    MarketStatusEvent,
    MintEvent,
    MintSource,
    SourceGenerations,
)


def _start_meta() -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-e3t2",
        config_hash="cfg-hash",
        source_provenance={"venue": "poly"},
        fixture_ids=(18209181,),
    )


def _mint_event(*, source: MintSource, source_epoch: int, recv_ts: int) -> MintEvent:
    return MintEvent(
        sequence_no=0,  # placeholder — the recorder reassigns the global sequence
        source=source,
        source_epoch=source_epoch,
        recv_ts=recv_ts,
    )


# --- typed per-source epoch resume (Produces 5b) -------------------------------------------


def test_resume_source_generations_typed_per_source(tmp_path):
    """Prior generations are READ from the durable ``MintEvent`` tape, then typed-incremented."""
    # Guard-ON tape: book=5, fv=2, market_status=3 durable generations.
    guard_on = tmp_path / "guard_on"
    rec = LiveRecorder(guard_on, _start_meta())
    mint(rec, _mint_event(source="book", source_epoch=5, recv_ts=1_000))
    mint(rec, _mint_event(source="fv", source_epoch=2, recv_ts=1_001))
    mint(rec, _mint_event(source="market_status", source_epoch=3, recv_ts=1_002))
    rec.close()
    meta_bytes_before = (guard_on / "meta.json").read_bytes()

    prior = read_durable_source_generations(guard_on)
    # read from the TAPE, not meta.json (meta carries no epoch field at all)
    assert prior == SourceGenerations(book_source_epoch=5, fv_source_epoch=2, market_status_epoch=3)
    assert (guard_on / "meta.json").read_bytes() == meta_bytes_before  # R3 meta untouched

    resumed = resume_source_generations(prior, guard_enabled=True)
    assert resumed.book_source_epoch == 6  # book ALWAYS increments
    assert resumed.fv_source_epoch == 3  # guard-on → fv increments
    assert resumed.market_status_epoch == 4  # present → increments

    # Guard-OFF tape: book only, no FV / status generations.
    guard_off = tmp_path / "guard_off"
    rec2 = LiveRecorder(guard_off, _start_meta())
    mint(rec2, _mint_event(source="book", source_epoch=5, recv_ts=1_000))
    rec2.close()

    prior_off = read_durable_source_generations(guard_off)
    assert prior_off == SourceGenerations(
        book_source_epoch=5, fv_source_epoch=None, market_status_epoch=None
    )
    resumed_off = resume_source_generations(prior_off, guard_enabled=False)
    assert resumed_off.book_source_epoch == 6
    assert resumed_off.fv_source_epoch is None  # guard-off ⇒ NO fv epoch anywhere
    assert resumed_off.market_status_epoch is None


# --- every source inherits the single global pair (Produces 1/4) ---------------------------


def test_every_mint_source_carries_global_pair(tmp_path):
    """book/FV/status/match/projection each expose the recorder-assigned (recv_ts, sequence_no, epoch)."""
    rec = LiveRecorder(tmp_path, _start_meta())
    sources: tuple[MintSource, ...] = (
        "book",
        "fv",
        "market_status",
        "match_state",
        "projection",
    )
    returned: dict[str, tuple[int, int]] = {}
    for i, src in enumerate(sources):
        returned[src] = mint(rec, _mint_event(source=src, source_epoch=i, recv_ts=2_000 + i))
    rec.close()

    _, events, _ = read_session(tmp_path)
    by_source = {e["source"]: e for e in events}
    assert set(by_source) == set(sources)
    for i, src in enumerate(sources):
        row = by_source[src]
        # each row carries the full (recv_ts, sequence_no, epoch) triple
        assert (row["recv_ts"], row["sequence_no"]) == returned[src]
        assert row["source_epoch"] == i


def test_global_sequence_is_monotonic_across_sources(tmp_path):
    """Five sources through ONE recorder → one strictly-increasing global sequence."""
    rec = LiveRecorder(tmp_path, _start_meta())
    pairs = [
        mint(rec, _mint_event(source=src, source_epoch=0, recv_ts=3_000 + i))
        for i, src in enumerate(
            ("book", "fv", "market_status", "match_state", "projection")
        )
    ]
    rec.close()
    seqs = [seq for _, seq in pairs]
    assert seqs == [1, 2, 3, 4, 5]
    assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))


# --- absent market-status rows fail closed to UNKNOWN (Produces 4) -------------------------


def test_absent_status_rows_yield_unknown_never_synthesized_active(tmp_path):
    """A tape without a ``MarketStatusEvent`` yields ``UNKNOWN`` — never a synthesized ``ACTIVE``."""
    absent = tmp_path / "absent"
    rec = LiveRecorder(absent, _start_meta())
    mint(rec, _mint_event(source="book", source_epoch=0, recv_ts=4_000))  # book only, no status
    rec.close()

    status = latest_market_status(absent)
    assert status.status == "UNKNOWN"
    assert status.recv_ts is None and status.epoch is None

    # non-vacuous: a tape WITH a definite status row reads that status back
    present = tmp_path / "present"
    rec2 = LiveRecorder(present, _start_meta())
    record_market_status(
        rec2,
        MarketStatusEvent(
            venue_market_ref="0xcond", status="ACTIVE", recv_ts=4_100, epoch=7
        ),
    )
    rec2.close()

    active = latest_market_status(present)
    assert active.status == "ACTIVE"
    assert active.recv_ts == 4_100 and active.epoch == 7
