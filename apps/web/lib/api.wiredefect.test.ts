import { describe, it, expect } from 'vitest';
import { adaptLeaderboard, adaptExecutionReceipts } from '@/lib/api';
import type * as W from '@/lib/wire';

// II-W · Wire-defect RED controls (adapter surface). Each `it` reproduces ONE distinct frontend↔
// backend SEMANTIC wire defect where the frontend view-model vocabulary DIVERGES from the real
// backend contract. The honesty invariant is identical across all: the UI renders the backend's
// truth VERBATIM, never a fabricated or mis-mapped value. Backend contract citations are inline.
//
// Backend cross-run LeaderboardRow vocabulary (veridex/api/schemas.py:52-54, derived in
// veridex/leaderboard.py):
//   eligibility_badge : "fully-proven" | "partially-proven" | "unproven"   (anchor-derived)
//   anchor_status     : "all-anchored" | "some-pending"    | "none-anchored"
//   source_mode       : "all-replay"   | "all-live"        | "mixed" | "unknown"
// The view-model (lib/contracts.ts) narrows each to a 2/3-value display vocabulary — the adapter
// MUST translate the real backend words, not silently fall through to a wrong default.

// A COMPLETE cross-run wire LeaderboardRow (backend all-numeric row) with overridable badge fields.
function wireRow(over: Partial<W.LeaderboardRow>): W.LeaderboardRow {
  return {
    rank: 1, agent_id: 'agent-x', runs: 3, avg_clv_bps: 14.2, total_clv_bps: 42, sim_pnl: 42,
    brier: 0.2, max_drawdown: -3, action_count: 6, valid_pct: 100, proof_mode: 'reproducible',
    eligibility_badge: 'unproven', anchor_status: 'none-anchored', source_mode: 'all-replay',
    valid_count: 6, clv_confidence: 'high', low_sample: false, ...over,
  };
}

describe('II-W defect 3 · adaptLeaderboard: an all-replay board renders `replay`, never a spurious `mixed`', () => {
  it('every row source_mode="all-replay" ⇒ view `replay` (never a fabricated mixed/live label)', () => {
    // Backend _summarize_source_mode returns "all-replay" when EVERY run used replay
    // (veridex/leaderboard.py:85-100). The board is single-source replay — it must say so.
    const rows = adaptLeaderboard({ rows: [wireRow({ source_mode: 'all-replay' }), wireRow({ source_mode: 'all-replay' })] });
    expect(rows.every((r) => r.source_mode === 'replay')).toBe(true);
    expect(rows.some((r) => r.source_mode === 'mixed')).toBe(false);
  });

  it('source_mode="all-live" ⇒ view `live` (the honest mapping of the other pure-source aggregate)', () => {
    const rows = adaptLeaderboard({ rows: [wireRow({ source_mode: 'all-live' })] });
    expect(rows[0].source_mode).toBe('live');
  });
});

describe('II-W defect 4 · adaptLeaderboard: anchor_status is backend-authoritative (v2.10.10 §A6)', () => {
  it('all-anchored ⇒ `anchored`; none-anchored ⇒ `not-anchored` (NEVER a fabricated green anchor)', () => {
    // Backend _summarize_anchor_status: "all-anchored" iff every run confirmed, "none-anchored" iff
    // none (veridex/leaderboard.py:66-82). Rendering all-anchored as not-anchored HIDES the real
    // anchor; rendering an absent/unconfirmed anchor as anchored FABRICATES one. Both are prohibited.
    const rows = adaptLeaderboard({
      rows: [wireRow({ anchor_status: 'all-anchored' }), wireRow({ anchor_status: 'none-anchored' })],
    });
    expect(rows[0].anchor_status).toBe('anchored');     // (a) verbatim truth: fully anchored
    expect(rows[1].anchor_status).not.toBe('anchored'); // (b) never a fake green anchor
    expect(rows[1].anchor_status).toBe('not-anchored');
  });
});

describe('II-W defect 5 · adaptLeaderboard: eligibility is the backend (anchor-derived) value, NEVER re-derived from proof_mode', () => {
  it('a fully-proven row renders `eligible` even when proof_mode would re-derive not-eligible', () => {
    // eligibility_badge is anchor-derived server-side (veridex/leaderboard.py:_eligibility_badge:
    // fully-proven iff every run anchored). It is NOT a function of proof_mode. This row is
    // fully-proven (⇒ eligible) but carries proof_mode="partial" — a client re-derivation from
    // proof_mode (as the competition adapter does) would WRONGLY say not-eligible. The backend wins.
    const rows = adaptLeaderboard({ rows: [wireRow({ eligibility_badge: 'fully-proven', proof_mode: 'partial' })] });
    expect(rows[0].eligibility_badge).toBe('eligible');
  });

  it('an unproven row renders `not-eligible` (the backend value, not a proof_mode guess)', () => {
    const rows = adaptLeaderboard({ rows: [wireRow({ eligibility_badge: 'unproven', proof_mode: 'verified' })] });
    expect(rows[0].eligibility_badge).toBe('not-eligible');
  });
});

describe('II-W defect 7 · adaptExecutionReceipts: nullable timestamp / execution state / receipt identity round-trip EXACTLY', () => {
  it('null→null timestamp, settled→filled (honest, no overstate), execution_id + venue_order_id verbatim', () => {
    // Backend ExecutionReceipt (veridex/execution/models.py:97-108): execution_id:str,
    // venue_order_id:str|None, submitted_at/settled_at: ISO str|None. ExecutionStatus enum
    // (models.py:26-39) is a SUPERSET of the view lane; `settled` folds to `filled` (never a later
    // fabricated stage). A null timestamp is honest "not yet" — it must survive as null.
    const receipts = adaptExecutionReceipts({
      receipts: [{
        execution_id: 'exec-77', venue: 'sxbet', market_ref: '1X2:FRA', side: 'back',
        requested_size: 50, filled_size: 0, price: 1.9, status: 'settled',
        venue_order_id: 'ord-77', mode: 'live_guarded',
        submitted_at: '2026-07-19T10:00:00Z', settled_at: null,
      }],
    });
    const r = receipts[0];
    expect(r.execution_id).toBe('exec-77');   // identity preserved verbatim
    expect(r.venue_order_id).toBe('ord-77');  // identity preserved verbatim
    expect(r.settled_at).toBeNull();          // null stays null (honest "not yet")
    expect(r.submitted_at).not.toBeNull();    // ISO string → epoch seconds (presence kept)
    expect(r.status).toBe('filled');          // settled → filled, never a fabricated later stage
  });
});
