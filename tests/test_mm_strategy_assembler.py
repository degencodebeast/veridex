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
    AssemblerOwnedFacts,
    CadenceRun,
    FvArrival,
    ObservationFactory,
    ObservationTick,
    latest_market_status,
    mint,
    project_guard_fv,
    read_durable_source_generations,
    record_market_status,
    resume_source_generations,
    run_cadence,
    sample_fv_into_mint,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    InventoryProjection,
    MarketStatusEvent,
    MintEvent,
    MintSource,
    SourceGenerations,
    StrategyObservation,
    StrategyState,
    StreamIdentity,
)
from veridex.mm_strategy.core import decide


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

    status = latest_market_status(absent, "0xcond")
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

    active = latest_market_status(present, "0xcond")
    assert active.status == "ACTIVE"
    assert active.recv_ts == 4_100 and active.epoch == 7


def test_replay_status_bound_to_requested_market(tmp_path):
    """Gate #2 MAJOR-3 (REPLAY): the status read must be BOUND to the requested market, not global.

    ``latest_market_status`` must return the latest durable row FOR THE REQUESTED
    ``venue_market_ref`` — never the globally-latest row. Tape: ``A=CLOSED`` (earlier) then
    ``B=ACTIVE`` (later, higher global ``sequence_no``). A query for A must return A's CLOSED, NOT
    B's globally-latest ACTIVE. A market with no row on the tape yields typed UNKNOWN (fail closed),
    never another market's row — a foreign ACTIVE can never be applied to A.
    """
    session = tmp_path / "two_markets"
    rec = LiveRecorder(session, _start_meta())
    record_market_status(
        rec,
        MarketStatusEvent(
            venue_market_ref="0xA", status="CLOSED", recv_ts=5_000, epoch=1
        ),
    )
    record_market_status(
        rec,
        MarketStatusEvent(
            venue_market_ref="0xB", status="ACTIVE", recv_ts=5_100, epoch=1
        ),
    )
    rec.close()

    # A-query returns A's OWN CLOSED row, not B's globally-latest ACTIVE.
    a_status = latest_market_status(session, "0xA")
    assert a_status.venue_market_ref == "0xA"
    assert a_status.status == "CLOSED"
    assert a_status.recv_ts == 5_000 and a_status.epoch == 1

    # B-query returns B's ACTIVE (non-vacuous: the market filter selects the right stream).
    b_status = latest_market_status(session, "0xB")
    assert b_status.venue_market_ref == "0xB"
    assert b_status.status == "ACTIVE"
    assert b_status.recv_ts == 5_100 and b_status.epoch == 1

    # A market with NO row on the tape → typed UNKNOWN for THAT market, never a foreign row.
    missing = latest_market_status(session, "0xC")
    assert missing.status == "UNKNOWN"
    assert missing.recv_ts is None and missing.epoch is None
    assert missing.venue_market_ref == "0xC"


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


# --- E3-T4: guard-off projection + FV-independent cadence -----------------------------------
# (REQ-020(d/d2) / AC-049 / AC-051 / AC-056 / RED-43 / RED-45 / RED-52)
#
# The load-bearing A/B baseline-arm integrity invariant. With the guard config-disabled the
# assembler emits ``guard_fv=None`` on EVERY observation regardless of FV feed health, so the
# baseline observation/decision/state stream is BYTE-IDENTICAL across healthy / stale / absent /
# reconnecting FV. Observation cadence is minted ONLY by non-FV events — an FV arrival feeds the
# single-authority latest-value cache alone, never minting an observation nor advancing the
# ``observation_sequence`` (in EITHER arm).


def _config(*, guard_enabled: bool) -> StrategyConfig:
    """A valid :class:`StrategyConfig` — ``guard_enabled`` is the sole REQUIRED knob."""
    return StrategyConfig(guard_enabled=guard_enabled)


# The single stream every legacy E3-T4 helper mints into — one fixture / market / side / token.
_ID_A = StreamIdentity(
    fixture_id=1, market_ref="TEAM-A/YES", side="YES", token_id="TOKEN-YES"
)


def _observation(
    *,
    observation_sequence: int,
    guard_fv: GuardFairValue | None,
    book_source_epoch: int = 1,
    as_of_ts: int = 1_000,
    identity: StreamIdentity = _ID_A,
) -> StrategyObservation:
    """One healthy per-tick observation; every ``recv_ts`` is ≤ ``as_of_ts`` (REQ-022 guard). The
    RAW venue / match-state facts are arm-INDEPENDENT — only ``guard_fv`` differs between arms.
    ``identity`` binds the observation's stream leg (a); it defaults to the single legacy stream."""
    recv = as_of_ts - 10
    return StrategyObservation(
        fixture_id=identity.fixture_id,
        market_ref=identity.market_ref,
        side=identity.side,
        token_id=identity.token_id,
        venue_market_ref="0xmarket",
        tick_size=0.01,
        observation_sequence=observation_sequence,
        book_source_epoch=book_source_epoch,
        bid=0.49,
        ask=0.51,
        bid_size=100.0,
        ask_size=120.0,
        book_status="ok",
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=guard_fv,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=1,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _owned(*, as_of_ts: int) -> AssemblerOwnedFacts:
    """The assembler-owned facts for one honest tick — EXACTLY what :func:`_observation` builds, so
    :func:`run_cadence`'s field-for-field authentication passes silently (Gate #2 MAJOR-1)."""
    recv = as_of_ts - 10
    return AssemblerOwnedFacts(
        book_source_epoch=1,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=1,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
    )


def _tick(
    source: MintSource, recv_ts: int, *, as_of_ts: int, identity: StreamIdentity = _ID_A
) -> ObservationTick:
    """A non-FV mint whose ``build`` factory binds the cadence-assigned sequence + projected guard
    leg onto arm-identical venue facts (``book_source_epoch`` held at 1 — no reset in this stream).
    ``owned`` declares the assembler-owned fields the driver authenticates the built observation
    against — the honest factory reproduces them verbatim, so authentication passes. ``identity``
    is the tick's stream; the honest factory builds an observation reporting that SAME identity."""

    def build(observation_sequence: int, guard_fv: GuardFairValue | None) -> StrategyObservation:
        return _observation(
            observation_sequence=observation_sequence,
            guard_fv=guard_fv,
            as_of_ts=as_of_ts,
            identity=identity,
        )

    return ObservationTick(
        source=source,
        source_epoch=1,
        recv_ts=recv_ts,
        owned=_owned(as_of_ts=as_of_ts),
        identity=identity,
        build=build,
    )


def _ticks() -> list[ObservationTick]:
    """The fixed non-FV cadence — book / match-state / projection, IDENTICAL across all scenarios."""
    return [
        _tick("book", 900, as_of_ts=1_000),
        _tick("match_state", 1_090, as_of_ts=1_100),
        _tick("projection", 1_190, as_of_ts=1_200),
    ]


# FV arrivals of differing feed health — none of which may touch a guard-OFF observation. All bind
# the single legacy stream identity ``_ID_A`` (guard-on single-stream selection is unchanged).
_FV_FRESH = FvArrival(source_ts=500, recv_ts=850, value=0.55, source_epoch=1, identity=_ID_A)
_FV_FRESH2 = FvArrival(source_ts=600, recv_ts=1_050, value=0.58, source_epoch=1, identity=_ID_A)
_FV_STALE = FvArrival(source_ts=1, recv_ts=800, value=0.20, source_epoch=1, identity=_ID_A)
_FV_RECON = FvArrival(source_ts=700, recv_ts=1_050, value=0.62, source_epoch=2, identity=_ID_A)


def _healthy_events() -> list[FvArrival | ObservationTick]:
    t = _ticks()
    return [_FV_FRESH, t[0], _FV_FRESH2, t[1], t[2]]


def _stale_events() -> list[FvArrival | ObservationTick]:
    t = _ticks()
    return [_FV_STALE, t[0], t[1], t[2]]


def _absent_events() -> list[FvArrival | ObservationTick]:
    return list(_ticks())


def _reconnecting_events() -> list[FvArrival | ObservationTick]:
    t = _ticks()
    return [_FV_FRESH, t[0], _FV_RECON, t[1], t[2]]


def _cadence(
    session_dir, events: list[FvArrival | ObservationTick], *, guard_enabled: bool
) -> CadenceRun:
    rec = LiveRecorder(session_dir, _start_meta())
    run = run_cadence(rec, events, guard_enabled=guard_enabled)
    rec.close()
    return run


def _fold(
    observations: tuple[StrategyObservation, ...], *, guard_enabled: bool
) -> tuple[tuple, StrategyState]:
    """Thread the PURE ``decide`` over the minted observation stream — decisions + final state."""
    config = _config(guard_enabled=guard_enabled)
    state = StrategyState()
    decisions = []
    for observation in observations:
        decision, state = decide(observation, state, config)
        decisions.append(decision)
    return tuple(decisions), state


def test_guard_off_identical_across_fv_health(tmp_path):
    """Guard-OFF: healthy / stale / absent / reconnecting FV → IDENTICAL counts, sequences, hashes,
    decisions AND venue-accumulator states (AC-051/056, RED-45/52)."""
    scenarios = {
        "absent": _absent_events(),
        "healthy": _healthy_events(),
        "stale": _stale_events(),
        "reconnecting": _reconnecting_events(),
    }
    runs = {
        name: _cadence(tmp_path / name, events, guard_enabled=False)
        for name, events in scenarios.items()
    }
    folds = {name: _fold(run.observations, guard_enabled=False) for name, run in runs.items()}

    base_run, base_fold = runs["absent"], folds["absent"]
    base_hashes = [o.observation_hash() for o in base_run.observations]
    # non-vacuous: the baseline stream really minted three observations and trained accumulators.
    assert len(base_run.observations) == 3
    assert base_fold[1].smoother_mid is not None

    for name in ("healthy", "stale", "reconnecting"):
        run, (decisions, state) = runs[name], folds[name]
        assert len(run.observations) == 3  # FV events minted NO observation
        assert tuple(o.observation_sequence for o in run.observations) == (1, 2, 3)
        assert [o.observation_hash() for o in run.observations] == base_hashes
        assert decisions == base_fold[0]  # identical decision stream (incl. decision_id)
        assert state.state_hash() == base_fold[1].state_hash()
        # venue accumulators byte-identical
        assert (state.smoother_mid, state.spread_ref_samples, state.depth_ref_samples) == (
            base_fold[1].smoother_mid,
            base_fold[1].spread_ref_samples,
            base_fold[1].depth_ref_samples,
        )
        # guard-off carries NO fv value / ts / epoch ANYWHERE — observation OR state.
        assert all(o.guard_fv is None for o in run.observations)
        assert state.guard_watermark is None


def test_baseline_arm_fv_gate_cannot_touch(tmp_path):
    """Guard-OFF: a STALE FV produces a stream BYTE-IDENTICAL to a healthy FV (AC-049, RED-43)."""
    healthy = _cadence(tmp_path / "healthy", _healthy_events(), guard_enabled=False)
    stale = _cadence(tmp_path / "stale", _stale_events(), guard_enabled=False)

    # byte-identical observation payloads (not merely equal hashes).
    assert [o.model_dump() for o in healthy.observations] == [
        o.model_dump() for o in stale.observations
    ]
    # the stale FV cannot touch the baseline decision / state either.
    healthy_fold, stale_fold = (
        _fold(healthy.observations, guard_enabled=False),
        _fold(stale.observations, guard_enabled=False),
    )
    assert healthy_fold[0] == stale_fold[0]
    assert healthy_fold[1].state_hash() == stale_fold[1].state_hash()


@pytest.mark.parametrize("guard_enabled", [False, True])
def test_fv_events_mint_no_observation(tmp_path, guard_enabled):
    """An FV mint event does NOT mint an observation nor advance ``observation_sequence`` — in EITHER
    arm. The FV row IS on the tape (single-authority cache fed through the ONE global recorder)."""
    t = _ticks()
    events: list[FvArrival | ObservationTick] = [t[0], _FV_FRESH, t[1]]
    run = _cadence(tmp_path, events, guard_enabled=guard_enabled)

    # only the TWO non-FV ticks minted observations; the interleaved FV advanced neither.
    assert len(run.observations) == 2
    assert tuple(o.observation_sequence for o in run.observations) == (1, 2)

    # the FV nonetheless rode the ONE global recorder — a single fv MintEvent row on the tape.
    _, tape, _ = read_session(tmp_path)
    fv_rows = [
        r for r in tape if r.get("event_type") == "MintEvent" and r.get("source") == "fv"
    ]
    assert len(fv_rows) == 1
    assert len(run.mint_pairs) == 2  # one sealed pair per minted observation, none for the FV


def test_guard_on_stream_varies_with_fv(tmp_path):
    """Non-vacuity: with the guard ON, a fresh vs absent FV yields a DIFFERENT observation stream —
    so guard-off byte-identity is a real projection, not a stream that ignores FV in both arms."""
    with_fv = _cadence(tmp_path / "on_fresh", _healthy_events(), guard_enabled=True)
    without = _cadence(tmp_path / "on_absent", _absent_events(), guard_enabled=True)

    assert [o.observation_hash() for o in with_fv.observations] != [
        o.observation_hash() for o in without.observations
    ]
    assert any(o.guard_fv is not None for o in with_fv.observations)
    assert all(o.guard_fv is None for o in without.observations)


def test_project_guard_fv_off_never_reads_cache():
    """Guard-OFF projection returns ``None`` WITHOUT consulting the cache — the byte-identity root."""
    from veridex.mm_strategy.assembler import _CachedFv

    # a fully-visible cache entry: if guard-off consulted it at all, this leg would surface.
    cache = [
        _CachedFv(
            point=FvPoint(source_ts=500, recv_ts=850, value=0.55, sequence_no=1),
            source_epoch=9,
            message_id="m",
            proof_status="proven",
            identity=_ID_A,
        )
    ]
    assert project_guard_fv(cache, (1_000, 5), identity=_ID_A, guard_enabled=False) is None
    # guard-ON over the same cache DOES surface the leg (non-vacuous contrast).
    leg = project_guard_fv(cache, (1_000, 5), identity=_ID_A, guard_enabled=True)
    assert leg is not None and leg.fv == 0.55 and leg.fv_source_epoch == 9


# --- Gate #2 MAJOR-1: the assembler AUTHENTICATES its owned observation fields ---------------
# (REQ-020b/027, AC-049/051/056; Codex external review)
#
# The observation FACTORY is trusted ONLY to assemble the arm-identical venue microstructure.
# ``run_cadence`` is the SOLE author of ``observation_sequence`` (the cadence counter) and the
# projected ``guard_fv`` leg, and the tick's ``AssemblerOwnedFacts`` declare the epoch / market-status
# / match-state values. A malicious or buggy factory that ignores the injected sequence, injects an FV
# leg under a guard-off run, or forges an epoch / status / match-state value MUST fail closed — the
# boundary is ENFORCED, not a convention the honest builder follows. These controls reproduce Codex's
# constructed malicious-factory attack against the actual module.


def _malicious_tick(
    build: ObservationFactory, *, source: MintSource = "book", as_of_ts: int = 1_000
) -> ObservationTick:
    """A tick whose honest ``owned`` facts are declared by the assembler, but whose ``build`` factory
    forges an assembler-owned field — the exact trust-boundary attack Codex constructed."""
    return ObservationTick(
        source=source,
        source_epoch=1,
        recv_ts=as_of_ts - 100,
        owned=_owned(as_of_ts=as_of_ts),
        identity=_ID_A,
        build=build,
    )


def test_malicious_factory_sequence_override_fails_closed(tmp_path):
    """A factory that ignores the cadence-assigned sequence and forges ``observation_sequence=999``
    MUST be rejected — ``run_cadence`` does NOT emit 999 (Codex MAJOR-1 control)."""

    def evil(observation_sequence: int, guard_fv: GuardFairValue | None) -> StrategyObservation:
        # ignore the injected sequence (which is 1 for the sole tick); forge 999.
        return _observation(observation_sequence=999, guard_fv=guard_fv, as_of_ts=1_000)

    rec = LiveRecorder(tmp_path, _start_meta())
    with pytest.raises(ValueError, match="observation_sequence"):
        run_cadence(rec, [_malicious_tick(evil)], guard_enabled=False)
    rec.close()


def test_malicious_factory_guard_off_fv_injection_fails_closed(tmp_path):
    """Under ``guard_enabled=False`` the projected leg is ALWAYS ``None``; a factory that injects a
    non-null ``GuardFairValue`` anyway MUST fail closed — guard-off carries NO FV element
    (Codex MAJOR-1 control; the byte-identity guarantee is now enforced, not conventional)."""
    injected = GuardFairValue(
        fv=0.61,
        fv_source_ts=500,
        fv_recv_ts=980,
        fv_source_epoch=1,
        message_id=None,
        proof_status="unavailable_no_message_id",
    )

    def evil(observation_sequence: int, guard_fv: GuardFairValue | None) -> StrategyObservation:
        # guard-off hands guard_fv=None; forge a non-null leg regardless.
        return _observation(
            observation_sequence=observation_sequence, guard_fv=injected, as_of_ts=1_000
        )

    rec = LiveRecorder(tmp_path, _start_meta())
    with pytest.raises(ValueError, match="guard_fv"):
        run_cadence(rec, [_malicious_tick(evil)], guard_enabled=False)
    rec.close()


@pytest.mark.parametrize(
    ("field_name", "overrides"),
    [
        ("book_source_epoch", {"book_source_epoch": 7}),
        ("market_status", {"market_status": "HALTED"}),
        ("market_status_epoch", {"market_status_epoch": 99}),
        ("phase", {"phase": 0}),
        ("suspended", {"suspended": True}),
        ("match_state_recv_ts", {"match_state_recv_ts": 123}),
    ],
)
def test_malicious_factory_epoch_status_matchstate_override_fails_closed(
    tmp_path, field_name, overrides
):
    """A factory that forges a source-epoch / market-status / match-state field DIFFERENT from the
    assembler's authoritative :class:`AssemblerOwnedFacts` MUST fail closed (Codex MAJOR-1)."""

    def evil(observation_sequence: int, guard_fv: GuardFairValue | None) -> StrategyObservation:
        base = _observation(
            observation_sequence=observation_sequence, guard_fv=guard_fv, as_of_ts=1_000
        )
        return base.model_copy(update=overrides)

    rec = LiveRecorder(tmp_path, _start_meta())
    with pytest.raises(ValueError, match=field_name):
        run_cadence(rec, [_malicious_tick(evil)], guard_enabled=False)
    rec.close()


# --- Gate #2 MAJOR-2: the FV cache is keyed by stream identity (no cross-stream broadcast) ----
# (REQ-020b/027, REQ-070; Codex external review)
#
# The FV latest-value cache must carry fixture/market/side/token identity and KEY selection by it,
# so a foreign outcome's fair value can never drive another outcome's residual pull. These controls
# reproduce Codex's constructed cross-market broadcast (one FV arrival, honest ticks for markets A
# and B, both emitting ``guard_fv=0.77``) and a cross-side variant against the actual module.

# A second market on the SAME fixture, and a same-market opposite side — the two foreign streams.
_ID_B = StreamIdentity(
    fixture_id=1, market_ref="TEAM-B/YES", side="YES", token_id="TOKEN-B-YES"
)
_ID_NO = StreamIdentity(
    fixture_id=1, market_ref="TEAM-A/YES", side="NO", token_id="TOKEN-NO"
)


def test_fv_does_not_leak_across_markets(tmp_path):
    """Codex's cross-market control: ONE FV arrival for market A (0.77) then honest guard-ON ticks
    for A and B → A's observation carries ``guard_fv=0.77``, B's carries ``None`` (NEVER 0.77).

    Without an identity key the process-global cache broadcasts A's fair value onto B's tick (both
    observations emitted 0.77); keying selection by stream identity makes A's FV INVISIBLE to B."""
    fv_a = FvArrival(source_ts=500, recv_ts=850, value=0.77, source_epoch=1, identity=_ID_A)
    tick_a = _tick("book", 900, as_of_ts=1_000, identity=_ID_A)
    tick_b = _tick("book", 1_090, as_of_ts=1_100, identity=_ID_B)

    run = _cadence(tmp_path, [fv_a, tick_a, tick_b], guard_enabled=True)
    by_market = {o.market_ref: o for o in run.observations}

    obs_a, obs_b = by_market["TEAM-A/YES"], by_market["TEAM-B/YES"]
    # A owns the FV — it surfaces on A's guard leg (non-vacuous: the cache IS consulted).
    assert obs_a.guard_fv is not None and obs_a.guard_fv.fv == 0.77
    # B is a FOREIGN stream — A's 0.77 must be invisible; B has no FV of its own.
    assert obs_b.guard_fv is None
    assert not (obs_b.guard_fv is not None and obs_b.guard_fv.fv == 0.77)

    # Per-stream correctness (not merely "B is always None"): give B its OWN FV and A keeps 0.77
    # while B now sees ONLY its own 0.33 — neither stream ever reads the other's value.
    fv_b = FvArrival(source_ts=600, recv_ts=1_000, value=0.33, source_epoch=1, identity=_ID_B)
    run2 = _cadence(
        tmp_path / "per_stream", [fv_a, tick_a, fv_b, tick_b], guard_enabled=True
    )
    by_market2 = {o.market_ref: o for o in run2.observations}
    a2, b2 = by_market2["TEAM-A/YES"], by_market2["TEAM-B/YES"]
    assert a2.guard_fv is not None and a2.guard_fv.fv == 0.77
    assert b2.guard_fv is not None and b2.guard_fv.fv == 0.33


def test_fv_does_not_leak_across_sides(tmp_path):
    """Cross-side variant: an FV for (market, side=YES) is NOT selected for (market, side=NO).

    A same-market opposite side is a distinct ``(market_ref, side)`` stream (REQ-070 ``raw_gap`` is
    per outcome); the YES fair value must never alias onto the NO minting tick."""
    fv_yes = FvArrival(source_ts=500, recv_ts=850, value=0.71, source_epoch=1, identity=_ID_A)
    tick_yes = _tick("book", 900, as_of_ts=1_000, identity=_ID_A)
    tick_no = _tick("book", 1_090, as_of_ts=1_100, identity=_ID_NO)

    run = _cadence(tmp_path, [fv_yes, tick_yes, tick_no], guard_enabled=True)
    by_side = {o.side: o for o in run.observations}

    obs_yes, obs_no = by_side["YES"], by_side["NO"]
    assert obs_yes.guard_fv is not None and obs_yes.guard_fv.fv == 0.71  # YES owns the FV
    assert obs_no.guard_fv is None  # NO is a foreign side — YES's 0.71 is invisible
