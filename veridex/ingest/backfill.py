"""EV-2 — historical backfill: TxLINE ``/odds/updates`` + ``/scores/updates`` -> ReplayPack.

PURE, no-network core (async-shell / sync-core split, CON-010): the async network shell that
actually fetches the histories lives in ``scripts/txline_live/backfill.py``; this module only
transforms already-fetched native payloads into a verified :class:`ReplayPack`.

T0 read-only-to-trust-path tool: NO network, NO LLM imports. It synthesizes a recorder session
(the T2 on-disk format) from the fetched odds movement history, then hands it to the SAME
:func:`~veridex.ingest.replay_pack.pack_from_session` converter live capture uses — so a
backfilled pack replays through the one projection, indistinguishable in shape from a
live-captured one.

Unlike live capture, backfill has no independent SSE stream: the ``/odds/updates`` movement
history *is* the record stream, so both the ``records.jsonl`` replay leg and the
``updates_<fid>.json`` closing-reconstruction leg (CON-040) derive from it.

Scores ride ALONGSIDE the pack as a NON-EVIDENCE sibling (``scores_<fid>.json``): the pack format
has no sealed scores slot, so scores are never hashed into ``content_hash`` / the evidence path —
they sit next to the pack as recorded context only.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from veridex.ingest.recorder import SessionMeta, envelope_line, finalize_meta
from veridex.ingest.replay_pack import ReplayPack, pack_from_session

_TOOL_VERSION = "backfill/1"
_ENDPOINTS = ["/odds/updates", "/scores/updates"]


def build_pack_from_fixture(
    fixture_id: int,
    odds_updates: list[dict[str, Any]],
    scores_updates: list[dict[str, Any]],
    out_dir: Path,
) -> ReplayPack:
    """Transform fetched TxLINE histories for one fixture into a verified :class:`ReplayPack`.

    Synthesizes a recorder session (T2 format) in a temp staging dir from ``odds_updates``, then
    runs it through :func:`~veridex.ingest.replay_pack.pack_from_session` — the SAME converter
    live capture uses. The resulting pack loads via
    :func:`~veridex.ingest.replay_pack.load_pack_marketstates` with ``verify=True``.

    ``odds_updates`` are filtered to those whose ``FixtureId`` matches ``fixture_id`` (a fetched
    history is per-fixture, but this stays robust if a caller passes a multi-fixture list), so the
    produced pack describes exactly this one fixture.

    Scores are written as a NON-EVIDENCE sibling (``out_dir/scores_<fixture_id>.json``) — the pack
    format has no sealed scores slot, so scores are never folded into ``content_hash``.

    Args:
        fixture_id: The fixture the pack is for.
        odds_updates: Native TxLINE odds messages (the ``/odds/updates`` movement history). Each
            carries ``FixtureId`` and ``Ts`` (epoch ms).
        scores_updates: Native TxLINE score messages (the ``/scores/updates`` history) — recorded
            as a sibling, not sealed evidence.
        out_dir: Directory to write the self-describing pack into.

    Returns:
        The built :class:`ReplayPack` (also serialized to ``out_dir/pack.json``).

    Raises:
        ValueError: If no ``odds_updates`` message matches ``fixture_id`` (nothing to replay).
    """
    fixture_odds = [msg for msg in odds_updates if int(msg["FixtureId"]) == fixture_id]
    if not fixture_odds:
        raise ValueError(f"no odds updates for fixture_id {fixture_id} in the {len(odds_updates)} supplied")

    timestamps = [int(msg["Ts"]) for msg in fixture_odds]
    started_ts, ended_ts = min(timestamps), max(timestamps)

    with tempfile.TemporaryDirectory() as staging:
        session_dir = Path(staging)

        # records.jsonl: each odds message enveloped with its own Ts as the receipt time.
        (session_dir / "records.jsonl").write_text(
            "".join(envelope_line(msg, int(msg["Ts"])) + "\n" for msg in fixture_odds)
        )

        # updates_<fid>.json: the raw movement history — the odds_updates leg pack_from_session
        # copies verbatim for CON-040 closing reconstruction.
        (session_dir / f"updates_{fixture_id}.json").write_text(json.dumps(fixture_odds))

        # meta.json: a finalized SessionMeta with this fixture's id + record count.
        start_meta = SessionMeta(started_ts=started_ts, endpoints=list(_ENDPOINTS), tool_version=_TOOL_VERSION)
        meta = finalize_meta(start_meta, ended_ts=ended_ts, record_counts={str(fixture_id): len(fixture_odds)})
        (session_dir / "meta.json").write_text(meta.model_dump_json())

        pack = pack_from_session(session_dir, out_dir)

    # Scores ride alongside as a NON-EVIDENCE sibling — written AFTER pack_from_session sealed the
    # content_hash, and never referenced by the manifest, so they can't enter the evidence path.
    (out_dir / f"scores_{fixture_id}.json").write_text(json.dumps(scores_updates))

    return pack
