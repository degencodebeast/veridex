import type {
  LeaderboardRow, CompetitionSummary, AgentSummary, AgentProfileRecord, RunSummary,
  RewardSummary, OpsAlert, OddsUpdate, FixtureSummary, RuntimeOverview, CanonicalLogLine,
  FeedHealthState,
} from '@/lib/catalog';

// NOTE: the production policy-envelope defaults (DEFAULT_POLICY_ENVELOPE / MM_POLICY_ENVELOPE) used
// to live here. They are shipped CONFIG, not demo entity fixtures, so they were relocated to
// `lib/config/policy.ts` (T-2) — letting the demo-path fixture-prohibition scan ban every entity
// fixture in this module without banning a legitimate production default. Import them from
// `@/lib/config/policy`.

// NOTE the highest avg_clv_bps belongs to a NOT-eligible agent on purpose (AC-005),
// and `stale_scout` is a deliberately low-sample row (WD-7). clv_confidence/low_sample
// are honest to valid_count (>=30 high, >=10 medium, else low) — never inflated.
export const LEADERBOARD_ROWS: LeaderboardRow[] = [
  {
    rank: 0, agent_id: 'momentum_fr', agent_name: 'Momentum FR', agent_kind: 'llm · momentum',
    runs: 8, avg_clv_bps: 24.6, total_clv_bps: 196.8, sim_pnl: 2.1, brier: 0.19, max_drawdown: -5.4,
    action_count: 74, valid_count: 51, valid_pct: 68.9, proof_mode: 'partial',
    eligibility_badge: 'not-eligible', anchor_status: 'pending', source_mode: 'live',
    clv_confidence: 'high', low_sample: false,
  },
  {
    rank: 0, agent_id: 'value_clv', agent_name: 'Value CLV', agent_kind: 'numeric · value_clv',
    runs: 14, avg_clv_bps: 18.4, total_clv_bps: 257.6, sim_pnl: 1.6, brier: 0.21, max_drawdown: -3.1,
    action_count: 120, valid_count: 114, valid_pct: 95.0, proof_mode: 'reproducible',
    eligibility_badge: 'eligible', anchor_status: 'anchored', source_mode: 'live',
    clv_confidence: 'high', low_sample: false,
  },
  {
    rank: 0, agent_id: 'baseline', agent_name: 'Baseline', agent_kind: 'numeric · baseline',
    runs: 14, avg_clv_bps: 6.2, total_clv_bps: 86.8, sim_pnl: 0.4, brier: 0.25, max_drawdown: -2.0,
    action_count: 112, valid_count: 108, valid_pct: 96.4, proof_mode: 'reproducible',
    eligibility_badge: 'eligible', anchor_status: 'anchored', source_mode: 'replay',
    clv_confidence: 'high', low_sample: false,
  },
  {
    rank: 0, agent_id: 'stale_scout', agent_name: 'Stale Scout', agent_kind: 'llm · stale_line',
    runs: 1, avg_clv_bps: 12.0, total_clv_bps: 12.0, sim_pnl: 0.3, brier: 0.22, max_drawdown: -1.1,
    action_count: 7, valid_count: 6, valid_pct: 85.7, proof_mode: 'verified',
    eligibility_badge: 'eligible', anchor_status: 'pending', source_mode: 'live',
    clv_confidence: 'low', low_sample: true,
  },
];

export const COMPETITIONS: CompetitionSummary[] = [
  {
    competition_id: 'wc-fra-bra', title: 'World Cup · FRA v BRA', competition_type: 'live_arena',
    lifecycle: 'live', source_mode: 'live', execution_mode: 'paper', proof_mode: 'verified',
    market_scope: '1X2 · O/U · AH', roster_size: 4, events_per_min: 11, ws_live: true, settled_run_id: null,
    demo_leader_clv_bps: 24.6,
  },
  {
    competition_id: 'wc-arg-ger', title: 'World Cup · ARG v GER', competition_type: 'replay_arena',
    lifecycle: 'upcoming', source_mode: 'replay', execution_mode: 'dry_run', proof_mode: 'reproducible',
    market_scope: '1X2 · O/U', roster_size: 3, events_per_min: null, ws_live: false, settled_run_id: null,
  },
  {
    competition_id: 'wc-esp-ned', title: 'World Cup · ESP v NED', competition_type: 'head_to_head',
    lifecycle: 'settled', source_mode: 'replay', execution_mode: 'paper', proof_mode: 'reproducible',
    market_scope: '1X2', roster_size: 2, events_per_min: null, ws_live: false, settled_run_id: 'run_esp_ned_01',
    demo_leader_clv_bps: 16.2,
  },
];

export const AGENTS: AgentSummary[] = [
  { agent_id: 'value_clv', agent_name: 'Value CLV', archetype: 'value_clv', mode: 'numeric', avg_clv_bps: 18.4, runs: 14, proof_mode: 'reproducible', source_mode: 'live', valid_pct: 95.0, source: 'STUDIO' },
  { agent_id: 'momentum_fr', agent_name: 'Momentum FR', archetype: 'momentum', mode: 'llm', avg_clv_bps: 24.6, runs: 8, proof_mode: 'partial', source_mode: 'live', valid_pct: 68.9, source: 'STUDIO' },
  { agent_id: 'baseline', agent_name: 'Baseline', archetype: 'baseline', mode: 'numeric', avg_clv_bps: 6.2, runs: 14, proof_mode: 'reproducible', source_mode: 'replay', valid_pct: 96.4, source: 'STUDIO' },
  { agent_id: 'byoa_hermes', agent_name: 'Hermes BYOA', archetype: 'contrarian', mode: 'llm', avg_clv_bps: 9.1, runs: 2, proof_mode: 'verified', source_mode: 'live', valid_pct: 80.0, source: 'BYOA' },
];

export const AGENT_PROFILES: Record<string, AgentProfileRecord> = {
  value_clv: {
    ...AGENTS[0], valid_count: 114, config_hash: '0xcfg_8a31f2', policy_hash: '0xpol_1b77ce',
    strategy_caption: 'Deterministic numeric agent: flags value when recomputed edge ≥ 8 bps on the de-vigged 1X2 consensus, within a 30s quote-age window. Describes configuration only.',
    completed_competitions: [
      { competition_id: 'wc-esp-ned', title: 'World Cup · ESP v NED', run_id: 'run_esp_ned_01', avg_clv_bps: 16.2 },
    ],
    anchors: [{ run_id: 'run_esp_ned_01', tx_signature: '5x7Af3qK…21bC', slot: 287340912 }],
    deployment_provenance: 'STUDIO · pinned 2026-06-21', total_clv_bps: 257.6, eligibility_badge: 'eligible',
  },
  momentum_fr: {
    ...AGENTS[1], valid_count: 51, config_hash: '0xcfg_55ab90', policy_hash: '0xpol_2c01de',
    strategy_caption: 'LLM agent (momentum archetype): proposes FOLLOW_MOMENTUM / FADE actions; the law recomputes every action. LLM rationale is never an input to score.',
    completed_competitions: [], anchors: [], deployment_provenance: 'STUDIO · pinned 2026-06-25',
    total_clv_bps: 196.8, eligibility_badge: 'not-eligible',
  },
};

export const MY_AGENTS: AgentSummary[] = [AGENTS[0], AGENTS[1], AGENTS[3]];

export const MY_RUNS: RunSummary[] = [
  { run_id: 'run_esp_ned_01', agent_id: 'value_clv', agent_name: 'Value CLV', avg_clv_bps: 16.2, proof_mode: 'reproducible', anchor_status: 'anchored', source_mode: 'replay' },
  { run_id: 'run_fra_bra_live', agent_id: 'momentum_fr', agent_name: 'Momentum FR', avg_clv_bps: 24.6, proof_mode: 'partial', anchor_status: 'pending', source_mode: 'live' },
];

export const MY_REWARDS: RewardSummary[] = [
  { competition_id: 'wc-esp-ned', title: 'World Cup · ESP v NED', amount_label: '— (design target)', payout_state: 'design-target' },
  { competition_id: 'wc-fra-bra', title: 'World Cup · FRA v BRA', amount_label: '— (pending settle)', payout_state: 'pending' },
];

export const ALERTS: OpsAlert[] = [
  { id: 'al1', kind: 'deny', agent_id: 'momentum_fr', message: 'POLICY DENY · edge 4 < 8 bps', ts: 1719655393000 },
  { id: 'al2', kind: 'hold', agent_id: 'byoa_hermes', message: 'HUMAN APPROVAL HOLD · stake ≥ 250', ts: 1719655400000 },
];

const nldMar: OddsUpdate[] = [
  { fixture_id: 18172280, message_id: 'm1', ts: 1, in_running: false, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['NLD', 'Draw', 'MAR'], prices: [1500, 3500, 6000], pct: ['66.667', '28.571', '16.667'] },
  { fixture_id: 18172280, message_id: 'm2', ts: 2, in_running: false, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['NLD', 'Draw', 'MAR'], prices: [1472, 3550, 6100], pct: ['67.935', '28.169', '16.393'] },
  { fixture_id: 18172280, message_id: 'm3', ts: 3, in_running: false, market_family: 'OVERUNDER_PARTICIPANT_GOALS', market_parameters: 'line=2.5', price_names: ['Over', 'Under'], prices: [1910, 1980], pct: ['52.356', '50.505'] },
  { fixture_id: 18172280, message_id: 'm4', ts: 4, in_running: false, market_family: 'ASIANHANDICAP_PARTICIPANT_GOALS', market_parameters: 'line=-0.25', price_names: ['NLD', 'MAR'], prices: [1880, 2010], pct: ['53.191', '49.751'] },
];

export const ODDS_UPDATES: Record<number, OddsUpdate[]> = { 18172280: nldMar };

export const FIXTURES: FixtureSummary[] = [
  { fixture_id: 18172280, competition: 'World Cup', participant1: 'NLD', participant2: 'MAR', start_time: '2026-06-29T18:00:00Z', in_running: true },
  { fixture_id: 18172281, competition: 'World Cup', participant1: 'ARG', participant2: 'GER', start_time: '2026-06-30T18:00:00Z', in_running: false },
];

// WD-4 feed-health DEMO default for the Markets right rail — honest replay/not-live telemetry
// (mirrors contracts/fixtures/feed_health.json). ws_live=false ⇒ the rail renders OFFLINE, never
// a fake "healthy/live"; staleness is real. This is DEMO data (the MockBanner labels it).
export const FEED_HEALTH: FeedHealthState = {
  source_mode: 'replay',
  ws_live: false,
  connected: false,
  txline_configured: false,
  events_per_min: null,
  ticks_seen: 128,
  staleness_s: 5,
  stale: false,
  fixture_id: 18172280,
  anchor_status: 'not-anchored',
  last_tick_ts: 1782518393,
};

export const RUNTIME_OVERVIEW: Record<string, RuntimeOverview> = {
  momentum_fr: {
    agent_id: 'momentum_fr', run_id: 'run_fra_bra_live', status: 'running',
    latest_model_latency_ms: 412, latest_model_tokens: 318, last_action: 'FOLLOW_MOMENTUM',
    schema_valid: true, errors: 0, retries: 1, tool_calls: 3, source: 'STUDIO',
  },
  byoa_hermes: {
    agent_id: 'byoa_hermes', run_id: 'run_byoa_01', status: 'running',
    latest_model_latency_ms: null, latest_model_tokens: null, last_action: 'WAIT',
    schema_valid: true, errors: 0, retries: 0, tool_calls: 0, source: 'BYOA',
  },
};

export const RUNTIME_LOG: CanonicalLogLine[] = [
  { ts: '14:03:13.917', channel: 'OPS', event: 'action_emitted', detail: 'BACK FRA 1X2 @ 2.38' },
  { ts: '14:03:13.941', channel: 'PROOF', event: 'law_recomputed', detail: 'close=2.30 · CLV +14.0 bps · valid' },
  { ts: '14:03:13.958', channel: 'POLICY', event: 'policy_result', detail: 'ALLOW · edge 14 ≥ 8 bps' },
  { ts: '14:03:14.002', channel: 'EXEC', event: 'submitted', detail: 'sxbet · paper · size 25' },
  { ts: '14:03:14.118', channel: 'OPS', event: 'model_call_completed', detail: 'latency 412ms · 318 tok' },
];
