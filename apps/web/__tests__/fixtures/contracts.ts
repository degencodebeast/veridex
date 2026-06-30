import type {
  CockpitState, InspectorRecord, ProofArtifact, VerifyResult, CheckResult,
} from '@/lib/contracts';

export const sampleChecks: CheckResult[] = [
  { id: 'evidence_integrity', label: 'Evidence Integrity', result: 'pass', severity: 'blocking', method: 'sha256-recompute', scope: 'run', evidence_refs: ['ev:0x91…'], rules: [] },
  { id: 'metrics_recomputed', label: 'Score Recomputed', result: 'pass', severity: 'blocking', method: 'law-recompute', scope: 'run', evidence_refs: [], rules: [] },
  { id: 'manifest_bound', label: 'Manifest Bound', result: 'pass', severity: 'blocking', method: 'config_hash', scope: 'run', evidence_refs: [], rules: [] },
  { id: 'llm_boundary', label: 'LLM Boundary', result: 'pass', severity: 'blocking', method: 'static-import-audit', scope: 'trust-path', evidence_refs: ['audit:imports'], rules: [] },
  { id: 'policy_obeyed', label: 'Policy Obeyed', result: 'pass', severity: 'blocking', method: 'envelope-replay', scope: 'run', evidence_refs: [], rules: [] },
  { id: 'receipt_separation', label: 'Receipt Separation', result: 'pass', severity: 'warning', method: 'hash-diff', scope: 'run', evidence_refs: [], rules: [] },
  // ANCHOR is illustratively pending — drives the no-hardcoded-PASS test (SEC-002/AC-002).
  { id: 'anchor', label: 'On-Chain Anchor', result: 'pending', severity: 'info', method: 'memo+txoracle', scope: 'batch', evidence_refs: [], rules: [] },
];

export const sampleProofArtifact: ProofArtifact = {
  run_id: 'run_7f3a',
  verifier_version: 'v1.4.2',
  proof_mode: 'reproducible',
  source_mode: 'replay',
  evidence_hash: '0x91ac3b8e7d2f4a10',
  manifest_hash: '0x4d2c8a01ffee9931',
  run_event_count: 142,
  schema_versions: { run_event: '2', market_state: '1' },
  chain: [
    { id: 'evidence', label: 'Evidence', sub: 'sealed RunEvents', hash: '0x91ac3b8e', status: 'pass' },
    { id: 'pre-score', label: 'Pre-Score', sub: 'raw prescore', hash: '0x2bd9', status: 'pass' },
    { id: 'score', label: 'Score', sub: 'law recompute', hash: '0x77fa', status: 'pass' },
    { id: 'manifest', label: 'Manifest', sub: 'config+policy', hash: '0x4d2c', status: 'pass' },
    { id: 'anchor', label: 'Anchor', sub: 'memo+txoracle', hash: '—', status: 'pending' },
  ],
  checks: sampleChecks,
  metrics: { clv_bps: 18.0, sim_pnl: 124.5, brier: 0.182, hit_rate: 0.61, max_drawdown: -42.0 },
  validations: [
    { method: 'validateOdds', data_kind: 'odds', message_id: 'msg_8812', result: 'pass', root: '0xroot_odds' },
    { method: 'validateFixtureBatch', data_kind: 'fixture', result: 'pass', root: '0xroot_fx' },
    { method: 'validateStat', data_kind: 'stat', message_id: 'msg_9001', result: 'pass', root: '0xroot_stat' },
  ],
  anchor: {
    status: 'pending', tx_signature: null, cluster: 'solana-devnet', slot: null, committed_at: null,
    batching_note: 'odds/scores batched on 5-min intervals; fixture validation is batch-root based',
    explorer_url: null,
  },
  proof_mode_map: { reproducible: 3, verified: 1, partial: 0 },
};

// Offline-replay variant: ANCHOR is not_applicable (AC-002).
export const offlineReplayProofArtifact: ProofArtifact = {
  ...sampleProofArtifact,
  checks: sampleProofArtifact.checks.map((c) =>
    c.id === 'anchor' ? { ...c, result: 'not_applicable' } : c),
  anchor: { ...sampleProofArtifact.anchor, status: 'not_applicable' },
};

export const sampleVerifyResult: VerifyResult = {
  ok: true,
  verified: true,
  evidence_hash_confirmed: true,
  manifest_hash_confirmed: true,
  recomputed: { recomputed_edge_bps: 22.0, clv_bps: 18.0, valid: true },
  manifest_hash: '0x4d2c8a01ffee9931',
  anchor_tx: '5xQ…anchorTx',
  explorer_url: 'https://explorer.solana.com/tx/5xQanchorTx?cluster=devnet',
  verifier_version: 'v1.4.2',
  checks: sampleChecks,
  metrics: { clv_bps: 18.0, sim_pnl: 124.5, brier: 0.182, hit_rate: 0.61, max_drawdown: -42.0 },
};

export const sampleInspectorRecord: InspectorRecord = {
  run_id: 'run_7f3a',
  agent_id: 'agt_momentum_3',
  action_seq: 87,
  proof_mode: 'verified',
  is_live: true,
  market_state: {
    fixture_id: 18172280, tick_seq: 87, ts: 1719663793, phase: 1,
    markets: { '1X2_PARTICIPANT_RESULT': { stable_prob_bps: { FRA: 6794, DRAW: 2100, BRA: 1106 }, stable_price: { FRA: 1.472, DRAW: 4.76, BRA: 9.04 }, suspended: false } },
    scores: { goals_h: 1, goals_a: 1, corners_h: 4, corners_a: 3 },
  },
  agent_action: {
    type: 'FLAG_VALUE',
    params: { market_key: '1X2_PARTICIPANT_RESULT', side: 'FRA', reason: 'home pressure rising', confidence: 0.72, claimed_edge_bps: 30 },
  },
  recompute: { recomputed_edge_bps: 22.0, clv_bps: 18.0, valid: true },
  clv_explanation: {
    entry_implied_pct: 67.9, delta_bps: 18.0, closing_implied_pct: 69.7, score_bps: 18.0,
    fair_value_pct: 67.9, closing_fair_value_pct: 69.7, venue_decimal_price: 1.472,
    executable_edge_bps: 22.0, clv_bps: 18.0, stake_fraction: 0.06,
    plain: 'Fair value 67.9% → closing 69.7%; executable edge +22.0 bps at venue 1.472; CLV +18.0 bps.',
  },
  untrusted_llm: { model: 'claude-sonnet-4-6', confidence: 0.72, claimed_edge_bps: 30, rationale: 'France controlling tempo; expect goal.' },
};

export const sampleCockpitState: CockpitState = {
  competition_id: 'wc-fra-bra',
  run_id: 'run_7f3a',
  header: { fixture: 'FRA v BRA', competition: 'World Cup', source_mode: 'live', execution_mode: 'paper', proof_mode: 'verified', events: 142, valid_pct: 93 },
  trace: [
    { stage: 'evidence', label: 'Evidence', state: 'done' },
    { stage: 'law', label: 'Law', state: 'done' },
    { stage: 'policy', label: 'Policy', state: 'done' },
    { stage: 'receipt', label: 'Receipt', state: 'active' },
    { stage: 'score', label: 'Score', state: 'active' },
    { stage: 'anchor', label: 'Anchor', state: 'pending' },
  ],
  match: { fixture: 'FRA v BRA', phase: 'H2', minute: 62, goals: [1, 1], yellow: [2, 1], red: [0, 0], corners: [4, 3], status: 'live' },
  leaderboard: [
    { rank: 1, agent_id: 'agt_momentum_3', agent_name: 'Momentum-3', agent_kind: 'LLM', runs: 12, avg_clv_bps: 21.4, total_clv_bps: 256.8, sim_pnl: 124.5, brier: 0.182, max_drawdown: -42.0, action_count: 87, valid_pct: 93, proof_mode: 'verified', eligibility_badge: 'eligible', anchor_status: 'pending', source_mode: 'live', valid_count: 81, clv_confidence: 'high', low_sample: false },
    { rank: 2, agent_id: 'agt_value_1', agent_name: 'Value-CLV-1', agent_kind: 'Numeric', runs: 12, avg_clv_bps: 19.8, total_clv_bps: 237.6, sim_pnl: 98.1, brier: 0.201, max_drawdown: -31.0, action_count: 64, valid_pct: 99, proof_mode: 'reproducible', eligibility_badge: 'eligible', anchor_status: 'anchored', source_mode: 'live', valid_count: 63, clv_confidence: 'high', low_sample: false },
  ],
  events: [
    { seq: 87, type: 'AGENT_ACTION', payload_hash: '0xa1b2c3d4', evidence: true, ts: 1719663793, agent_id: 'agt_momentum_3', summary: 'FLAG_VALUE FRA 1X2 @ 1.472' },
    { seq: 88, type: 'law_recomputed', payload_hash: '0xb2c3d4e5', evidence: true, ts: 1719663794, summary: 'edge 22 bps · valid' },
    { seq: 89, type: 'policy_result', payload_hash: '0xc3d4e5f6', evidence: false, ts: 1719663794, summary: 'ALLOW · 22 ≥ 8 bps' },
    { seq: 90, type: 'score_update', payload_hash: '0xd4e5f607', evidence: false, ts: 1719663795, summary: 'CLV +18.0 bps' },
  ],
  receipts: [
    { execution_id: 'ex_01', venue: 'SX Bet', market_ref: '1X2:FRA', side: 'FRA', requested_size: 100, filled_size: 100, price: 1.472, status: 'filled', venue_order_id: 'sx_771', mode: 'paper', submitted_at: 1719663796, settled_at: 1719663797 },
  ],
  policy: [
    { tick_seq: 89, decision: 'ALLOW', reason: 'edge ≥ min', edge_bps: 22, min_edge_bps: 8 },
  ],
  kill_armed: true,
};
