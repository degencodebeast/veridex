// Fixture-seeded REPLAY cockpit projection (DEMO). Body only — getCockpitState merges the honest
// header (source demoted to replay). REPLAY is labeled REPLAY; the MATCH phase (in-play) is a
// SEPARATE fixture-fact axis from source_mode. No fabricated LIVE. This demo projection is what the
// Cockpit screen renders under mock; the real WS stream replaces it when wired (B→A).
//
// Cockpit B/C split (match panel): goals/yellow/red/corners/phase = (B) WIREABLE — TxLINE's soccer
// feed provides them (stat-keys 1-8 + the 19-value phase enum); Veridex just stubs scores={} today
// (txline_normalize.py:117), so LIVE is honest-empty "pending scores-feed" (MatchStatePanel). The
// running minute/clock ("67'") = (C) genuinely absent — TxLINE tracks WHICH HALF, not elapsed time;
// the screen never implies a live clock. ROADMAP: wire the TxLINE /scores/stream normalizer
// (stat-keys 1-8 + 19-phase enum → MarketState.scores/phase) to turn goals/cards/corners/phase from
// demo into real on-chain-verifiable data.
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import { rankByAvgClv } from '@/lib/derive';
import type { CockpitState } from '@/lib/contracts';

export type CockpitProjectionBody = Pick<
  CockpitState, 'trace' | 'match' | 'leaderboard' | 'events' | 'receipts' | 'policy' | 'kill_armed'
>;

export const COCKPIT_DEMO: CockpitProjectionBody = {
  // Proof-trace progression mid-run: evidence→law→policy→receipt done, score active, anchor pending.
  trace: [
    { stage: 'evidence', label: 'Evidence sealed', state: 'done' },
    { stage: 'law', label: 'Law recompute', state: 'done' },
    { stage: 'policy', label: 'Policy gate', state: 'done' },
    { stage: 'receipt', label: 'Execution receipt', state: 'done' },
    { stage: 'score', label: 'CLV score', state: 'active' },
    { stage: 'anchor', label: 'Solana anchor', state: 'pending' },
  ],
  // Match phase is IN-PLAY (a fixture fact) — this is NOT a source-mode claim (source is REPLAY).
  // NOTE (fan-out): the backend has NO MatchState object — only MarketState.phase (int) + scores
  // (stat→value dict); there is NO match clock and NO in_running field. So this ENTIRE rich match
  // panel (minute/goals/cards/corners) is DEMO-only. In live the match is honest-absent
  // (getCockpitState → emptyMatch: minute null, zeros); the screen must NOT imply the WS fills a
  // clock or these structured stats.
  match: {
    fixture: 'FRA v BRA', phase: 'H2', minute: 67, goals: [1, 1],
    yellow: [2, 1], red: [0, 0], corners: [5, 3], status: 'live', coverage: 'replay · demo',
  },
  // Reuse the real catalog rows, demoted to REPLAY (the cockpit demo never shows a LIVE source).
  // NOTE (fan-out): the LIVE per-competition wire is `CompetitionLeaderboardRow` {rank, agent_id,
  // total_clv_bps, mean_clv_bps (NOT avg_clv_bps), valid_count, proof_mode} — SMALLER than the
  // cross-run LeaderboardRow view-model this demo reuses. Wire→view is an adapter GAP to bridge when
  // the cockpit leaderboard is wired live.
  // Pre-rank + pre-order the demo board exactly as the backend would (rank 1..n by Avg CLV desc):
  // ClvLeaderboard renders `rank` + order VERBATIM now (F-5 — no local re-sort), so the fixture, like
  // the real competition-scoped response, must carry the authoritative rank rather than rely on the view.
  leaderboard: rankByAvgClv(
    LEADERBOARD_ROWS.map((r) => ({ ...r, source_mode: r.source_mode === 'live' ? 'replay' : r.source_mode })),
  ),
  // Canonical event stream (seq desc). `evidence` = sealed-evidence prefix (AGENT_ACTION / law) vs
  // the derived non-scoring tail (scores / receipts / anchor). Mirrors CanonicalEvent's contract.
  events: [
    { seq: 1284, type: 'score_update', payload_hash: 'a1b2c3', evidence: false, ts: 1782518400, agent_id: 'value_clv', summary: 'CLV +18.4 bps (rolling)' },
    { seq: 1283, type: 'proof_anchor', payload_hash: 'd4e5f6', evidence: false, ts: 1782518398, summary: 'anchor pending · batched' },
    { seq: 1282, type: 'execution_receipt', payload_hash: '07a8b9', evidence: false, ts: 1782518396, agent_id: 'value_clv', summary: 'paper fill · FRA 1.472' },
    { seq: 1281, type: 'policy_result', payload_hash: 'c0d1e2', evidence: false, ts: 1782518394, agent_id: 'value_clv', summary: 'ALLOW · edge 14 ≥ 8 bps' },
    { seq: 1280, type: 'law_recomputed', payload_hash: 'f3a4b5', evidence: true, ts: 1782518392, agent_id: 'value_clv', summary: 'edge recomputed +14 bps' },
    { seq: 1279, type: 'AGENT_ACTION', payload_hash: '6c7d8e', evidence: true, ts: 1782518390, agent_id: 'value_clv', summary: 'FLAG_VALUE · FRA 1X2' },
    { seq: 1278, type: 'policy_result', payload_hash: '9f0a1b', evidence: false, ts: 1782518388, agent_id: 'momentum_fr', summary: 'DENY · edge 4 < 8 bps' },
    { seq: 1277, type: 'AGENT_ACTION', payload_hash: '2c3d4e', evidence: true, ts: 1782518386, agent_id: 'momentum_fr', summary: 'FOLLOW_MOMENTUM · Over 2.5' },
  ],
  receipts: [
    { execution_id: 'exec_demo_01', venue: 'sxbet', market_ref: '1X2:FRA', side: 'back', requested_size: 100, filled_size: 100, price: 1.472, status: 'filled', venue_order_id: 'demo-ord-01', mode: 'paper', submitted_at: 1782518396, settled_at: 1782518397 },
    { execution_id: 'exec_demo_02', venue: 'sxbet', market_ref: 'OU2.5:Over', side: 'back', requested_size: 80, filled_size: 0, price: 1.91, status: 'proposed', venue_order_id: null, mode: 'paper', submitted_at: null, settled_at: null },
  ],
  policy: [
    { tick_seq: 1281, decision: 'ALLOW', reason: 'edge ≥ min_edge', edge_bps: 14, min_edge_bps: 8 },
    { tick_seq: 1278, decision: 'DENY', reason: 'edge < min_edge', edge_bps: 4, min_edge_bps: 8 },
  ],
  kill_armed: false,
};
