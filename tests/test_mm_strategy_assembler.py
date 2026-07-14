"""E3-T2 tests: recorder-global mint envelope + typed per-source epoch resume (MM-R4-B).

The assembler is the SOLE author of per-source generation epochs (REQ-020b/027). It mints every
source through the ONE global :class:`~veridex.live_recorder.recorder.LiveRecorder` sequence
authority, reads the durable-max per-source generations back from the ``MintEvent`` tape rows (never
from ``meta.json``), and increments them on an assembler restart. Absent market-status rows fail
closed to ``UNKNOWN`` — the harness never synthesizes ``ACTIVE``.
"""

import pytest

from veridex.live_recorder.alignment import FvPoint
from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import read_session, read_session_strict
from veridex.mm_strategy.assembler import (
    latest_market_status,
    mint,
    read_durable_source_generations,
    record_market_status,
    resume_source_generations,
    sample_fv_into_mint,
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


# --- E3-T3: pair-aware FV cache sampling at the sealed global mint boundary -----------------
# (REQ-020(d2) / REQ-027(cache) / AC-058 / RED-54)


def test_pair_used_by_eligible_fv_pair_equals_disk_equals_post_restart(tmp_path):
    """3-way control: the pair fed to ``eligible_fv_pair`` == the disk pair == the post-restart pair.

    The assembler mints a non-FV trigger through the ONE global recorder; ``sample_fv_into_mint``
    hands that recorder-SEALED ``(recv_ts, sequence_no)`` to ``eligible_fv_pair`` as the visibility
    boundary. That decision boundary must be EXACTLY the pair persisted to ``records.jsonl`` and the
    EXACT pair a strict reload returns after a restart — the live decision boundary IS the sealed
    replay boundary, never a parallel guess (Codex-plan-review-R2 MAJOR-1; AC-058/RED-54).
    """
    rec = LiveRecorder(tmp_path, _start_meta())
    # a raw FV arrival cache (corrections retained) — the sampling input.
    fv_cache = [
        FvPoint(source_ts=100, recv_ts=1_000, value=0.55, sequence_no=1),
        FvPoint(source_ts=101, recv_ts=1_100, value=0.58, sequence_no=2),
    ]
    trigger = _mint_event(source="book", source_epoch=0, recv_ts=1_200)

    # (1) the pair the assembler feeds eligible_fv_pair (returned alongside the sampled point).
    decision_pair, sampled = sample_fv_into_mint(rec, trigger, fv_cache)
    rec.close()
    assert sampled is not None and sampled.value == 0.58  # freshest visible FV below the pair

    # (2) the pair record_and_return_pair wrote to disk (via the lenient reader).
    _, events, _ = read_session(tmp_path)
    book_row = next(e for e in events if e.get("source") == "book")
    disk_pair = (book_row["recv_ts"], book_row["sequence_no"])

    # (3) the pair a strict RELOAD returns after a restart (fresh read off the sealed tape).
    _, strict_events, _ = read_session_strict(tmp_path)
    book_row_reloaded = next(e for e in strict_events if e.get("source") == "book")
    restart_pair = (book_row_reloaded["recv_ts"], book_row_reloaded["sequence_no"])

    assert decision_pair == disk_pair == restart_pair


@pytest.mark.parametrize("trigger_source", ["book", "market_status", "match_state", "projection"])
def test_mint_boundary_visible_below_sealed_pair_across_triggers(tmp_path, trigger_source):
    """Every non-FV trigger samples the FV cache at ITS OWN sealed pair — same-ms-after stays hidden.

    AC-058 extends the FV-before/FV-after-at-equal-ms control to every mint source. A trigger minted
    at ``recv_ts`` seals a global ``sequence_no``; a same-millisecond FV that arrived just BEFORE
    (seq-1) is visible while one that arrived just AFTER (seq+1) is not — even with a fresher source.
    """
    session = tmp_path / trigger_source
    rec = LiveRecorder(session, _start_meta())
    trig_recv, trig_seq = mint(
        rec, _mint_event(source=trigger_source, source_epoch=0, recv_ts=1_200)
    )
    rec.close()

    # same millisecond as the trigger; the global sequence is what orders them.
    fv_before = FvPoint(source_ts=100, recv_ts=trig_recv, value=0.5, sequence_no=trig_seq - 1)
    fv_after = FvPoint(source_ts=200, recv_ts=trig_recv, value=0.9, sequence_no=trig_seq + 1)

    from veridex.live_recorder.alignment import eligible_fv_pair

    got = eligible_fv_pair([fv_before, fv_after], mint_recv_ts=trig_recv, mint_sequence_no=trig_seq)
    assert got is fv_before  # BEFORE (seq-1) visible; AFTER (seq+1, fresher source) invisible
    assert got.value == 0.5
