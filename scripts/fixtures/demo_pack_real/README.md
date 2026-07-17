# demo_pack_real — the banked GENUINE World Cup demo ReplayPack (I-10)

This is a tamper-evident [`ReplayPack`](../../../veridex/ingest/replay_pack.py) of **genuine TxLINE
odds** — the real FIFA World Cup 2026 quarter-final fixtures. It is the judge replay experience and
the Sharp-Momentum (II-10) gate input. The shipped `../demo_pack/` stays as the SYNTHETIC CI/fallback.

## Provenance (honest)

- **`provenance: "genuine-txline"`** — genuine TxLINE odds, **not** synthetic and **not** a Polymarket
  book capture. Reads `genuine` through R-0a's [`is_genuine_pack`](../../../veridex/ingest/capture_chain.py).
- **`evidence_rung: "backfilled-price-history"`** — transparent about HOW it was captured: a REST
  `/odds/updates` backfill (via `scripts/txline_live/backfill.py`, real credentials), **not** a live
  `/odds/stream` SSE recording. Each record is a verbatim native TxLINE record with a sealed
  `MessageId`/`Ts` provable against the txoracle Solana root. See
  [`EvidenceRung`](../../../veridex/provenance.py).
- **Curated** — the first 400 verbatim records per fixture (a bounded, contiguous, chronological
  prefix) so the committed artifact stays small (~0.6 MB); the full multi-GB raw
  `scripts/txline_live/packs/` tree is gitignored. No record is fabricated or altered.

## Fixtures (from `scripts/txline_live/wc-qf-fixtures.json`)

| fixture_id | match |
|-----------|-------|
| 18209181 | France – Morocco |
| 18213979 | Norway – England |
| 18218149 | Spain – Belgium |
| 18222446 | Argentina – Switzerland |

## Tamper-evidence

`content_hash` is PINNED as `DEMO_PACK_REAL_CONTENT_HASH` in `scripts/demo_phase2d.py`. A mutated data
file, or an edited pin, fails `resolve_real_demo_pack()` (fail-closed). Regenerate deterministically
with `scripts/fixtures/build_demo_pack_real.py` (requires the raw WC packs present).

## Still owed to R-1

A **live `/odds/stream` recording** (the `recorded-live-quote` rung) is still owed for any roster gate
that strictly requires a live-captured session. This pack is genuine TxLINE odds at the
backfilled-price-history rung, sufficient to run the Sharp-Momentum harness honestly.
