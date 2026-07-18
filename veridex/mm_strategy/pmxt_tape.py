"""``pmxt-txline-mm-18209181-v1`` — the REAL-DATA maker replay tape (Polymarket depth + TxLINE FV).

The provenance-correct real-data artifact banked into
:data:`veridex.mm_strategy.session_factory.MM_TAPE_CATALOG`. It joins TWO independently-recorded real
in-play feeds for ONE match and replays them, dry-run, through the SAME cadence engine live trading
uses — with the TxLINE fair value ACTUALLY gating quotes when the guard is on.

HONEST LABEL (converge on this exact string; nothing here overclaims):

    "Real recorded in-play market data — Polymarket 10-level order-book depth + TxLINE 1X2 fair value
    — FIFA World Cup France v Morocco (fixture 18209181), replayed dry-run (no live money);
    cross-recorder-clock alignment seconds-scale (no sub-2s claim); research-grade v1 pack (not
    R3-sealed)."

VERDICT: **RESEARCH-GRADE REAL DATA** (not GENUINE-sealed, not a fabricated HYBRID). Both legs are
verbatim recorded rows; the join and the orchestration scaffolding are DERIVED by explicit, disclosed
rules; nothing is invented.

TWO REAL LEGS
-------------
* **FV leg — REAL TxLINE 1X2 fair value.** A byte-preserved, content-hash-verified ReplayPack SUB-
  SLICE lives at ``pmxt_tape_data/txline_pack/`` (the ``1X2_PARTICIPANT_RESULT||`` in-play records for
  this window, sliced verbatim from ``scripts/txline_live/packs/18209181/``). It is replayed through
  ``veridex.ingest.replay_pack.load_pack_marketstates(..., verify=True)`` — the SAME normalizer live
  TxLINE uses. ``FvArrival.value = stable_prob_bps["part1"] / 1e4`` (home / France); ``source_ts`` is
  the pack ``Ts`` normalized to seconds (``Ts // 1000``); ``message_id`` is the real pack ``MessageId``.
* **Book leg — REAL Polymarket 10-level depth.** A byte-preserved contiguous in-play slice of
  ``18209181_home_win.depth.jsonl`` lives at ``pmxt_tape_data/18209181_home_win.depth.slice.jsonl``.
  Each ``ObservationTick`` carries the REAL top-of-book (``best_bid``/``best_ask``), the REAL top-level
  sizes, and a REAL :data:`level_count_in_band` counted from the actual 10-level ladder — never a
  gate-clearing constant.

THE JOIN (no look-ahead)
------------------------
FvArrival + book ObservationTick events are MERGED by their real receive time (ms) and folded through
:func:`veridex.mm_strategy.assembler.run_cadence`, whose ``project_guard_fv`` selects the FV visible at
each book tick's SEALED global ``(recv_ts, sequence_no)`` mint boundary via
:func:`veridex.live_recorder.alignment.eligible_fv_pair` — the recorder-clock sibling of the proven
``veridex/maker/tape.py::_aligned_mid`` bisect join (most-recent-at-or-before, abstain when nothing is
visible, NEVER imputed). A book tick can therefore only ever see an FV that truly arrived at or before
it. The freshness bound is the strategy's own ``fv_freshness_ms`` (10 s), which — combined with the
seconds-scale cross-clock skew — is why the guard honestly abstains (``txline_stale``) on some frames.

FIELD-PROVENANCE CLASSIFICATION (every FvArrival / ObservationTick field)
-------------------------------------------------------------------------
CAPTURED = verbatim from a real recorded row. DERIVED = inferred from captured data by a stated rule.
UNAVAILABLE = no such datum exists in this artifact.

  FvArrival (TxLINE 1X2 fair value):
    * value                 CAPTURED   — pack ``stable_prob_bps["part1"] / 1e4`` (home/France FV),
                                         via the SAME normalizer live TxLINE uses.
    * source_ts             CAPTURED   — pack ``Ts`` normalized to unix SECONDS (``Ts // 1000``).
    * recv_ts               DERIVED    — the pack ``Ts`` in MILLISECONDS (the FV's OWN source clock,
                                         single-authority at replay). NOT ``book_recv_ts - 1`` (the SX-
                                         hybrid defect): the FV clock is decoupled from any book tick.
    * source_epoch          DERIVED    — constant ``1``: one generation (no FV reconnect in-window).
    * identity.fixture_id   CAPTURED   — ``18209181``.
    * identity.side         DERIVED    — ``"home"`` (the TxLINE ``part1`` -> venue ``home`` bridge).
    * identity.market_ref   DERIVED    — label built from the registry event slug (no round/stage).
    * identity.token_id     DERIVED    — stable stream key ``pmxt:18209181:home_win``.
    * message_id            CAPTURED   — the real pack ``MessageId`` (present on every record).
    * proof_status          DERIVED    — ``"absent"``: a real ``message_id`` anchor EXISTS (so NOT the
                                         ``unavailable_no_message_id`` sentinel), but NO Merkle inclusion
                                         proof was resolved — the v1 pack carries no proof evidence and
                                         this offline replay issues no ``/odds/validation`` query. This
                                         deliberately does NOT claim ``"proven"`` (see honesty boundary).
  ObservationTick (+ the observation its ``build`` factory assembles):
    * bid / ask             CAPTURED   — row ``best_bid`` / ``best_ask`` (real top-of-book).
    * bid_size / ask_size   CAPTURED   — row ``bids[0][1]`` / ``asks[0][1]`` (real top-level sizes).
    * level_count_in_band   CAPTURED*  — COUNTED from the real 10-level ladder: levels (bid or ask) with
                                         positive size within ``+-`` :data:`_LEVEL_BAND` of the row mid.
                                         Data-derived; a genuinely thin frame yields a low count and an
                                         honest ``book_thin`` abstention.
    * tick_size             CAPTURED   — row ``tick_size`` (the recorded Polymarket price grid).
    * book_recv_ts          CAPTURED   — row ``recv_ts_ms`` (pmxt recorder clock, milliseconds).
    * as_of_ts              DERIVED    — ``book_recv_ts +`` :data:`_DECISION_LATENCY_MS` (so the
                                         REQ-022 ``recv_ts <= as_of_ts`` guard holds).
    * source                DERIVED    — ``"book"``: a depth snapshot is the observation-minting trigger.
    * phase                 DERIVED    — ``1`` (in-play): the ENTIRE window is post-kickoff AND the
                                         paired TxLINE records are ``InRunning=true``. Not a blind
                                         placeholder — substantiated by kickoff_ts + InRunning.
    * suspended             DERIVED    — ``False``: the TxLINE 1X2 market is priced (not suspended) and
                                         the book is a live two-sided ladder across the window.
    * market_status         DERIVED    — ``"ACTIVE"`` by the same live-two-sided-book + in-play rule.
    * market_status_recv_ts DERIVED    — the SAME captured book instant (no separate status feed).
      / match_state_recv_ts
    * book_source_epoch     DERIVED    — constant ``1`` (single generation; no book reconnect in-window).
    * market_status_epoch   DERIVED    — constant ``1`` (single generation).
    * order_stream_ok / projection_fresh / inventory
                            DERIVED    — a fresh, flat, healthy OFFLINE projection (a replay has no live
                                         inventory or order stream).
  UNAVAILABLE (absent from this artifact — never fabricated):
    * cryptographic-genuine evidence rung — the committed sub-pack is ``pack_version=1`` = a DATA-ONLY
      content hash (``replay_pack.py`` §_compute_content_hash: a v1 pack "can never read genuine"). This
      tape is research-grade real recorded data, NOT an R3-sealed / cryptographically-genuine capture.

HONESTY BOUNDARIES (hard)
-------------------------
* **v1 pack.** The FV sub-pack is data-only-hashed (v1). No sealed / genuine evidence-rung claim is set.
* **Cross-recorder clocks.** The Polymarket depth (pmxt recorder clock) and the TxLINE FV (pack source
  clock) are DIFFERENT clocks; alignment is SECONDS-SCALE. This tape NEVER claims a sub-2s lead/lag nor
  a single-clock causal latency. The conservative ``fv_freshness_ms`` bound absorbs the skew.
* **No economic claim.** The matched guard-OFF/ON test REPORTS the behavior difference (the guard
  abstains on stale FV) only — there is no matched markout/PnL accounting, so no edge is claimed.

SELF-WARMING: the tape folds from the deploy's DEFAULT cold ``StrategyState()``; the first real book
observations warm the rolling spread/depth references (and, guard-on, the residual basis) from real
rows — no hand-authored seed. Proven end-to-end in ``tests/test_pmxt_txline_tape.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.mm_strategy.assembler import (
    AssemblerOwnedFacts,
    FvArrival,
    ObservationTick,
)
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    InventoryProjection,
    StrategyObservation,
    StreamIdentity,
)

if TYPE_CHECKING:
    from veridex.mm_strategy.session_factory import MakerReplayTape

#: The production catalog key this tape is banked under. Keyed by the real Polymarket/TxLINE fixture id;
#: claims NO round / stage (the registry carries no stage field).
TAPE_REF = "pmxt-txline-mm-18209181-v1"

# --- VERIFIED provenance (source: scripts/txline_live/wc-qf-fixtures.json + the two recorded feeds) ---
#: The real fixture id (NOT the synthetic ``fixture_id=1``, NOT the SX-hybrid ``18213979``).
FIXTURE_ID = 18209181
#: Registry event slug (``fifwc`` = FIFA World Cup) — teams + competition ARE substantiated there.
EVENT_SLUG = "fifwc-fra-mar-2026-07-09"
COMPETITION = "FIFA World Cup"
HOME_TEAM = "France"
AWAY_TEAM = "Morocco"
#: Verified kickoff (registry ``kickoff_ts``, seconds). The committed slices are entirely POST-kickoff.
KICKOFF_TS = 1783627200
#: The book venue (order book) and the fair-value signal.
VENUE = "Polymarket"
FV_SOURCE = "TxLINE 1X2 fair value"
#: The quoted outcome: the 1X2 HOME line (France to win, mid ~0.60) — the TxLINE ``part1`` side.
ROLE = "home"
TXLINE_SIDE = "part1"

#: Stream identifiers. ``market_ref`` names the REAL match via the registry event slug (no round/stage).
MARKET_REF = "pmxt:fifwc-fra-mar-2026-07-09:1x2"
SIDE = "home"
TOKEN_ID = "pmxt:18209181:home_win"
#: The REAL Polymarket market/outcome references recorded on every depth row (verbatim provenance).
VENUE_MARKET_REF = "0xc09537a0976d0927901432859fbb6dfe5d23d1d69bb4e8355253e7b142a44e83"  # condition_id
CONDITION_ID = "0xc09537a0976d0927901432859fbb6dfe5d23d1d69bb4e8355253e7b142a44e83"
ASSET_ID = "59376244379747988336832016917908236721650516433890704080422157946407884434153"

#: The TxLINE 1X2 full-match market key the FV is read under (the SAME key the normalizer emits).
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: A decision clock modeled a few ms after the captured book receive time (so ``recv_ts <= as_of_ts``).
_DECISION_LATENCY_MS = 10

#: The symmetric price band (native prob) the real ladder's ``level_count_in_band`` is counted within.
#: Disclosed constant (matches the default ``half_spread``); the COUNT itself is data-derived per row.
_LEVEL_BAND = 0.02

#: Committed, byte-preserved real slices (next to this module).
_DATA_DIR = Path(__file__).parent / "pmxt_tape_data"
_PACK_DIR = _DATA_DIR / "txline_pack"
_DEPTH_SLICE = _DATA_DIR / "18209181_home_win.depth.slice.jsonl"


def pack_dir() -> Path:
    """The committed, content-hash-verified TxLINE FV sub-pack directory."""
    return _PACK_DIR


def tape_identity() -> StreamIdentity:
    """The tape's stream identity — the real Polymarket/TxLINE HOME outcome of fixture 18209181."""
    return StreamIdentity(
        fixture_id=FIXTURE_ID,
        market_ref=MARKET_REF,
        side=SIDE,
        token_id=TOKEN_ID,
    )


def load_depth_slice(path: Path = _DEPTH_SLICE) -> tuple[dict[str, Any], ...]:
    """Load the committed byte-preserved Polymarket 10-level depth slice (verbatim recorded rows)."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    if not rows:
        raise ValueError(f"pmxt depth slice is empty: {path}")
    return tuple(rows)


def _load_fv_arrivals() -> list[FvArrival]:
    """Build the deduped TxLINE FV arrivals from the content-hash-verified sub-pack (M2/M3).

    The FV VALUE + source SECONDS come from the trusted normalizer (``load_pack_marketstates`` — the
    same projection live TxLINE uses, ``verify=True``); the ``message_id`` + millisecond source ``Ts``
    come from the byte-identical raw records (paired 1:1, ``batch_size=1``). A new arrival is emitted
    ONLY when the recorded home fair value CHANGES — repeated identical snapshots never masquerade as
    new TxLINE updates (M3).
    """
    states = load_pack_marketstates(_PACK_DIR, FIXTURE_ID, verify=True)
    raw = [
        json.loads(line)
        for line in (_PACK_DIR / "odds_18209181.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if len(states) != len(raw):
        raise ValueError(
            f"FV pack state/record count mismatch: {len(states)} states vs {len(raw)} records"
        )

    identity = tape_identity()
    arrivals: list[FvArrival] = []
    last_value: float | None = None
    for state, record in zip(states, raw, strict=True):
        market = state.markets.get(_TXLINE_1X2_FULL_MARKET_KEY)
        if not market:
            continue
        stable_prob_bps = market.get("stable_prob_bps") or {}
        bps = stable_prob_bps.get(TXLINE_SIDE)
        if bps is None:
            continue  # side unpriced on this tick — skip (never impute FV)
        value = bps / 1e4
        if last_value is not None and round(value, 6) == round(last_value, 6):
            continue  # M3 dedup: identical fair value is not a new TxLINE update
        last_value = value
        message_id = record.get("MessageId")
        arrivals.append(
            FvArrival(
                # M2: source_ts is SECONDS (state.ts == Ts // 1000); recv_ts is the FV's OWN ms source
                # clock (the real pack Ts), decoupled from any book tick — never book_recv_ts - 1.
                source_ts=state.ts,
                recv_ts=int(record["Ts"]),
                value=value,
                source_epoch=1,
                identity=identity,
                message_id=message_id,
                # message_id is present ⇒ NOT the "unavailable" sentinel; "absent" == a real anchor with
                # no resolved Merkle proof (v1 pack, offline replay). Never claims "proven".
                proof_status="absent" if message_id is not None else "unavailable_no_message_id",
            )
        )
    if not arrivals:
        raise ValueError("pmxt FV sub-pack produced no 1X2 fair-value arrivals")
    return arrivals


def _level_count_in_band(row: dict[str, Any]) -> int:
    """Count REAL ladder levels (bid or ask, positive size) within ``+-_LEVEL_BAND`` of the row mid (M4)."""
    mid = row["mid"]
    band = _LEVEL_BAND
    bids = sum(1 for price, size in row["bids"] if size > 0 and abs(price - mid) <= band)
    asks = sum(1 for price, size in row["asks"] if size > 0 and abs(price - mid) <= band)
    return bids + asks


def _observation(
    *,
    observation_sequence: int,
    guard_fv: GuardFairValue | None,
    book_recv_ts: int,
    as_of_ts: int,
    identity: StreamIdentity,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    tick_size: float,
    level_count_in_band: int,
) -> StrategyObservation:
    """One observation built from a real depth row (CAPTURED microstructure + DERIVED scaffolding).

    Every assembler-owned field is derived from the SAME row so it matches :func:`_owned` EXACTLY and
    ``run_cadence`` field-for-field authentication passes (REQ-020b/027) — the honest-builder recipe.
    """
    return StrategyObservation(
        fixture_id=identity.fixture_id,
        market_ref=identity.market_ref,
        side=identity.side,
        token_id=identity.token_id,
        venue_market_ref=VENUE_MARKET_REF,
        tick_size=tick_size,
        observation_sequence=observation_sequence,
        book_source_epoch=1,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        book_status="ok",
        status_reason=None,
        book_recv_ts=book_recv_ts,
        level_count_in_band=level_count_in_band,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=book_recv_ts,
        guard_fv=guard_fv,
        market_status="ACTIVE",
        market_status_recv_ts=book_recv_ts,
        market_status_epoch=1,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _owned(*, book_recv_ts: int) -> AssemblerOwnedFacts:
    """The assembler-owned facts EXACTLY matching :func:`_observation`, so authentication passes."""
    return AssemblerOwnedFacts(
        book_source_epoch=1,
        market_status="ACTIVE",
        market_status_recv_ts=book_recv_ts,
        market_status_epoch=1,
        phase=1,
        suspended=False,
        match_state_recv_ts=book_recv_ts,
    )


def _tick(row: dict[str, Any], identity: StreamIdentity) -> ObservationTick | None:
    """Build a ``book`` :class:`ObservationTick` from one real depth row, or ``None`` for a degraded row."""
    if row.get("mid") is None or row.get("best_bid") is None or row.get("best_ask") is None:
        return None  # degraded book row — no usable two-sided quote (honest skip, never imputed)
    if not row.get("bids") or not row.get("asks"):
        return None
    book_recv_ts = int(row["recv_ts_ms"])
    as_of_ts = book_recv_ts + _DECISION_LATENCY_MS
    bid = float(row["best_bid"])
    ask = float(row["best_ask"])
    bid_size = float(row["bids"][0][1])
    ask_size = float(row["asks"][0][1])
    tick_size = float(row["tick_size"])
    level_count_in_band = _level_count_in_band(row)

    def build(
        observation_sequence: int, guard_fv: GuardFairValue | None
    ) -> StrategyObservation:
        return _observation(
            observation_sequence=observation_sequence,
            guard_fv=guard_fv,
            book_recv_ts=book_recv_ts,
            as_of_ts=as_of_ts,
            identity=identity,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            tick_size=tick_size,
            level_count_in_band=level_count_in_band,
        )

    return ObservationTick(
        source="book",
        source_epoch=1,
        recv_ts=book_recv_ts,
        owned=_owned(book_recv_ts=book_recv_ts),
        identity=identity,
        build=build,
    )


def build_tape_events() -> tuple[FvArrival | ObservationTick, ...]:
    """Merge the real TxLINE FV arrivals + real Polymarket book ticks into the ordered cadence stream.

    Events are ordered by their real receive time (ms); ties place the FV arrival BEFORE the book tick
    so ``run_cadence``'s sealed-pair ``eligible_fv_pair`` selection realizes a no-look-ahead as-of join
    (the recorder-clock sibling of ``_aligned_mid``). Deterministic order + values keep
    :func:`veridex.mm_strategy.session_factory.compute_tape_content_hash` stable.
    """
    identity = tape_identity()
    fv_arrivals = _load_fv_arrivals()
    ticks = [t for t in (_tick(row, identity) for row in load_depth_slice()) if t is not None]
    if not ticks:
        raise ValueError("pmxt depth slice produced no usable book ticks")

    merged: list[tuple[int, int, FvArrival | ObservationTick]] = []
    for arrival in fv_arrivals:
        merged.append((arrival.recv_ts, 0, arrival))  # 0 == FV sorts before a same-ms book tick
    for tick in ticks:
        merged.append((tick.recv_ts, 1, tick))
    merged.sort(key=lambda item: (item[0], item[1]))
    return tuple(event for _ts, _kind, event in merged)


def build_pmxt_txline_tape() -> MakerReplayTape:
    """Build the ``pmxt-txline-mm-18209181-v1`` tape (the ``MM_TAPE_CATALOG`` factory value).

    The ``content_hash`` is computed from the actual events via the SAME canonical serializer
    ``reconstruct_mm_session`` re-verifies at resolve time, so a tampered resolved tape fails closed.
    Imports the carrier + hasher LAZILY to avoid an import cycle with ``session_factory``.
    """
    from veridex.mm_strategy.session_factory import (
        MakerReplayTape,
        compute_tape_content_hash,
    )

    events = build_tape_events()
    return MakerReplayTape(
        tape_ref=TAPE_REF,
        identity=tape_identity(),
        venue_market_ref=VENUE_MARKET_REF,
        events=events,
        content_hash=compute_tape_content_hash(events),
    )
