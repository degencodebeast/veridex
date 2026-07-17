"""I-10 — (re)bank the REAL World Cup demo ReplayPack under ``scripts/fixtures/demo_pack_real/``.

PROVENANCE HONESTY. This bakes a curated slice of GENUINE TxLINE odds — the real FIFA World Cup
2026 quarter-final fixtures listed in ``scripts/txline_live/wc-qf-fixtures.json`` — into a small,
committable, tamper-evident :class:`~veridex.ingest.replay_pack.ReplayPack`. Every odds record is a
VERBATIM native TxLINE record (never fabricated or altered); the curation is only a bounded,
contiguous, chronological PREFIX per fixture so the banked artifact stays small enough to commit
(the full raw ``scripts/txline_live/packs/`` tree is multi-GB and gitignored).

Genuineness evidence (why this is stamped ``genuine-txline``):
  * The source packs were produced by ``scripts/txline_live/backfill.py`` fetching the real TxLINE
    ``/odds/updates`` + ``/scores/updates`` endpoints with real credentials (``require_txline``) —
    NOT synthetic, NOT a Polymarket book capture.
  * The records carry ``Bookmaker="TXLineStablePriceDemargined"`` and per-record ``MessageId``/``Ts``
    that are SEALED and provable against the txoracle Solana root via ``/odds/validation``.
  * The fixture ids match ``wc-qf-fixtures.json`` (France-Morocco, Spain-Belgium, Norway-England,
    Argentina-Switzerland).

Transparency (why it ALSO records the precise evidence rung): this is a REST ``/odds/updates``
BACKFILL, not a live ``/odds/stream`` SSE recording — so the pack self-declares
``evidence_rung="backfilled-price-history"`` (a distinct, recognized genuine-TxLINE rung; see
:class:`veridex.provenance.EvidenceRung`). It reads ``genuine-txline`` (it is genuine TxLINE odds),
and it never over-claims to be a live-recorded-quote tape.

Reproducible + deterministic: the frozen :func:`~veridex.ingest.replay_pack.pack_from_session`
transform and R-0a's :func:`~veridex.ingest.capture_chain.stamp_pack_provenance` do all the work;
this script only selects verbatim records and stamps honest metadata, so re-running it over the same
raw packs reproduces the SAME ``content_hash`` pinned in ``scripts/demo_phase2d.py``.

Run (operator, with the raw WC packs present):
    .venv/bin/python scripts/fixtures/build_demo_pack_real.py
    # -> prints the content_hash to pin as DEMO_PACK_REAL_CONTENT_HASH in scripts/demo_phase2d.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.ingest.capture_chain import GENUINE_TXLINE_PROVENANCE, stamp_pack_provenance  # noqa: E402
from veridex.ingest.recorder import SessionMeta, envelope_line  # noqa: E402
from veridex.ingest.replay_pack import ReplayPack, pack_from_session, verify_content_hash  # noqa: E402
from veridex.provenance import EvidenceRung  # noqa: E402

#: The banked destination (committed, small, tamper-evident).
DEMO_PACK_REAL_DIR = ROOT / "scripts" / "fixtures" / "demo_pack_real"
#: Raw backfilled WC packs (gitignored, multi-GB) the demo pack is curated FROM.
RAW_PACKS_DIR = ROOT / "scripts" / "txline_live" / "packs"
#: The real World Cup quarter-final fixtures to bank (must exist under RAW_PACKS_DIR).
WC_FIXTURE_IDS: tuple[int, ...] = (18209181, 18218149, 18213979, 18222446)
#: Bounded, contiguous, chronological PREFIX per fixture — keeps the committed pack small while
#: every retained record is a verbatim genuine TxLINE record.
RECORDS_PER_FIXTURE = 400


def _read_verbatim_prefix(fixture_id: int, limit: int) -> list[dict[str, Any]]:
    """Read the first ``limit`` VERBATIM native TxLINE records for ``fixture_id`` from its raw pack.

    Raises:
        FileNotFoundError: If the raw backfilled pack for ``fixture_id`` is absent (the operator
            must have the gitignored ``scripts/txline_live/packs/`` tree present to rebuild).
        ValueError: If the raw pack yields no records (a genuine pack always has records).
    """
    odds_path = RAW_PACKS_DIR / str(fixture_id) / f"odds_{fixture_id}.jsonl"
    if not odds_path.exists():
        raise FileNotFoundError(
            f"raw WC pack missing for fixture {fixture_id}: {odds_path} — the multi-GB "
            f"scripts/txline_live/packs/ tree (gitignored) must be present to rebuild the demo pack"
        )
    records: list[dict[str, Any]] = []
    with odds_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
            if len(records) >= limit:
                break
    if not records:
        raise ValueError(f"raw WC pack for fixture {fixture_id} yielded no records")
    return records


def build(dst: Path = DEMO_PACK_REAL_DIR) -> ReplayPack:
    """Bank the curated genuine WC demo pack at ``dst`` and stamp it honestly; return the ReplayPack.

    The pack is stamped ``genuine-txline`` (R-0a's genuine provenance — it IS genuine TxLINE odds)
    with ``test_capture=False``, PLUS a transparent ``evidence_rung="backfilled-price-history"`` and a
    ``capture_method`` note so no reader mistakes this REST backfill for a live SSE recording.
    """
    all_records: list[dict[str, Any]] = []
    for fixture_id in WC_FIXTURE_IDS:
        all_records.extend(_read_verbatim_prefix(fixture_id, RECORDS_PER_FIXTURE))

    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        session_dir.mkdir()
        # Envelope each verbatim record with its OWN Ts as the receipt time (a faithful ordering).
        lines = [envelope_line(rec, int(rec["Ts"])) for rec in all_records]
        (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
        (session_dir / "meta.json").write_text(
            SessionMeta(
                started_ts=int(all_records[0]["Ts"]),
                endpoints=["/odds/updates"],
                tool_version="demo-pack-real/1 (curated WC /odds/updates backfill prefix)",
            ).model_dump_json()
        )
        if dst.exists():
            shutil.rmtree(dst)
        pack_from_session(session_dir, dst)  # re-read post-stamp below (stamping is not hashed)

    # R-0a's genuine stamp — this IS genuine TxLINE odds (not synthetic, not Polymarket).
    stamp_pack_provenance(dst, GENUINE_TXLINE_PROVENANCE, test_capture=False)
    # Transparent, NON-hashed capture metadata: the precise evidence rung + how it was captured.
    pack_path = dst / "pack.json"
    pack_doc = json.loads(pack_path.read_text())
    pack_doc["capture"]["evidence_rung"] = EvidenceRung.BACKFILLED_PRICE_HISTORY.value
    pack_doc["capture"]["capture_method"] = "odds-updates-backfill"
    pack_doc["capture"]["curation"] = (
        f"first {RECORDS_PER_FIXTURE} verbatim records per fixture "
        f"({', '.join(str(fid) for fid in WC_FIXTURE_IDS)}) — FIFA WC 2026 quarter-finals"
    )
    pack_path.write_text(json.dumps(pack_doc))

    if not verify_content_hash(dst):  # fail-closed: never ship a pack that fails its own hash
        raise RuntimeError(f"banked demo pack failed content_hash verification: {dst}")
    return ReplayPack.model_validate_json((dst / "pack.json").read_text())


def main() -> None:
    pack = build()
    fixture_ids = sorted(f["fixture_id"] for f in pack.fixtures)
    print(f"banked {DEMO_PACK_REAL_DIR}")
    print(f"  fixtures    : {fixture_ids} ({len(fixture_ids)} distinct WC QF fixtures)")
    print(f"  content_hash: {pack.content_hash}")
    print("  -> pin this as DEMO_PACK_REAL_CONTENT_HASH in scripts/demo_phase2d.py")


if __name__ == "__main__":
    main()
