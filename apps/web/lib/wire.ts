// THE contract binding. Mirrors contracts/veridex_api.contract.ts EXACTLY (the
// frozen Plan A wire shapes). Do not diverge: per-fixture parse tests in
// lib/wire.test.ts assert every contracts/fixtures/*.json parses into these types.
// The frontend view-model (lib/contracts.ts) is mapped from these in lib/api.ts.
import type { CheckId } from '@/lib/checks';

export type { CheckId };
export type CheckStatus = 'pass' | 'fail' | 'pending' | 'not_applicable';
export type CheckSeverity = 'blocking' | 'warning' | 'info';

export interface CheckResult {
  id: CheckId;
  label: string;
  result: CheckStatus;
  severity: CheckSeverity;
  method: string;
  scope: string;
  evidence_refs: string[];
  rules: Record<string, unknown>[];
  details: Record<string, unknown>;
  error: string | null;
}

export interface ProofArtifact {
  verifier_version: string;
  run: Record<string, unknown>;
  lineage: Record<string, unknown>;
  evidence: { evidence_hash: string; run_event_count: number };
  // SEC-001 target: the 7 CheckId only; CLV lives in `metrics` (see migration note).
  checks: Record<CheckId, CheckResult>;
  anchor: { status: string; signature: string | null; cluster: string | null };
  metrics?: PerformanceMetrics | null;
}

export interface PerformanceMetrics {
  clv: number | null;
  sim_pnl: number | null;
  brier: number | null;
  max_drawdown: number | null;
  hit_rate: number | null;
  scored_actions: number;
  per_agent: Record<string, unknown>[];
}

export interface VerifyResult {
  run_id: string;
  verified: boolean;
  evidence_hash: string;
  recomputed_evidence_hash: string;
  manifest_hash: string;
  checks: Record<CheckId, CheckResult>;
  metrics: PerformanceMetrics | null;
  anchor: Record<string, unknown>;
  proof_card: ProofArtifact;
}

export interface LeaderboardRow {
  rank: number;
  agent_id: string;
  runs: number;
  avg_clv_bps: number | null;
  total_clv_bps: number;
  sim_pnl: number;
  brier: number | null;
  max_drawdown: number;
  action_count: number;
  valid_pct: number;
  proof_mode: string;
  eligibility_badge: string;
  anchor_status: string;
  source_mode: string;
  // WD-7 CLV confidence (display-only — NEVER a rank input, SEC-005):
  valid_count: number;
  clv_confidence: string;
  low_sample: boolean;
}
export interface LeaderboardResponse {
  rows: LeaderboardRow[];
}

// GET /competitions/{id} — the contract's `CockpitState`, renamed here to avoid a
// clash with the frontend view-model `CockpitState` in lib/contracts.ts.
export interface CompetitionStateResponse {
  competition_id: string;
  status: string;
  config: Record<string, unknown>;
  roster: Record<string, unknown>[];
  leaderboard: Record<string, unknown>[];
  latest_seq: number;
  anchor_status: string;
  run_id: string | null;
  proof_card: ProofArtifact | null;
  execution: Record<string, unknown> | null;
}

export interface InspectorRecord {
  run_id: string;
  agent_id: string;
  tick_seq: number;
  market_state: Record<string, unknown>;
  agent_action: Record<string, unknown>;
  recompute: Record<string, unknown>;
  clv_bps: number | string;
  // "NOT AN INPUT TO SCORE" (SEC-007):
  untrusted_llm_metadata: Record<string, unknown>;
}

export interface FeedHealth {
  // GET /feed/health — read-only telemetry (NOT scored).
  source_mode: string;
  events_per_min: number | null;
  ws_live: boolean;
  last_tick_ts: number | null;
  anchor_status: string;
  // WD-4 staleness view (additive — ws_live mirrors connected):
  txline_configured: boolean;
  connected: boolean;
  ticks_seen: number;
  fixture_id: number | null;
  staleness_s: number | null;
  stale: boolean;
}

export type RuntimeEventType =
  | 'run_started' | 'status_changed' | 'action_emitted' | 'schema_validation'
  | 'run_completed' | 'run_failed' | 'model_call_started' | 'model_call_completed'
  | 'token_usage' | 'latency' | 'tool_call' | 'retry' | 'error' | 'trace_link';

export interface RuntimeEvent {
  type: RuntimeEventType;
  agent_id: string;
  run_id: string | null;
  session_id: string | null;
  ts: number;
  channel: 'OPS';
  payload: Record<string, unknown>;
}
export interface RuntimeEventsResponse {
  events: RuntimeEvent[];
}

// ---- MAKER LANE (maker_arena_result.v1) ----
// The sealed MAKER envelope (GET /maker/arena-result). SEC-005 at the boundary: these are a
// SEPARATE, `Maker*`-prefixed type family — they MUST NOT reuse LeaderboardRow/LeaderboardResponse/
// ProofArtifact. The maker lane ranks on `avg_toxicity_loss_bps` (lower is better, `asc`), NOT on
// any directional CLV. `real_executable_edge_bps` is ALWAYS `null` (no fill/PnL claim — honesty).

export interface MakerFalsificationWire {
  delta_bps: number;
  ci_low_bps: number;
  ci_high_bps: number;
  verdict: string;
  headline: string;
}

export interface MakerWindowClvAnalogWire {
  window_markout_bps: number;
  window_action_count: number;
  note: string;
}

// One agent's quote-quality row. `maker_rank` (NOT `rank`) is the maker-lane placement.
// `real_executable_edge_bps` is typed `null` — never a number (no fill/PnL claim, SEC-005).
export interface MakerLeaderboardRowWire {
  agent_id: string;
  avg_markout_bps: number;       // diagnostic, NOT the rank axis
  avg_toxicity_loss_bps: number; // THE rank axis (asc — lower is better)
  quote_count: number;
  scored: number;
  abstained: number;
  excluded: Record<string, unknown>;
  real_executable_edge_bps: null;
  maker_rank: number;
}

// Per-agent aggregate row (same quote-quality fields, but NO `maker_rank` — ranking is applied
// only in `maker_leaderboard`).
export interface MakerPerAgentWire {
  agent_id: string;
  avg_markout_bps: number;
  avg_toxicity_loss_bps: number;
  quote_count: number;
  scored: number;
  abstained: number;
  excluded: Record<string, unknown>;
  real_executable_edge_bps: null;
}

export interface MakerArenaResultWire {
  protocol_id: string;
  config_hash: string;
  rung: string; // e.g. "MM-R1"
  fixtures: number[];
  per_agent: MakerPerAgentWire[];
  maker_leaderboard: MakerLeaderboardRowWire[];
  falsification: MakerFalsificationWire;
  trade_aware_diagnostic: Record<string, unknown> | null;
  markout_adverse_decomposition: Record<string, unknown> | null;
  event_gate_timeline: Record<string, unknown> | null;
  window_clv_analog: MakerWindowClvAnalogWire;
  real_executable_edge_bps: null; // top-level: always null (no fill/PnL claim)
  fixture_universe_n: number;
  small_n_flag: boolean;
  excluded_by_reason: Record<string, unknown>;
  r2_bracket: Record<string, unknown> | null;
}

export interface MakerProofCardWire {
  rung: string;
  uncalibrated: boolean;
  headline: string;
  window_clv_analog: MakerWindowClvAnalogWire;
  falsification: MakerFalsificationWire;
  n_fixtures: number;
  small_n_note: string;
  trades_not_fills_caveat: string | null;
  trade_aware_diagnostic_note: string | null;
  r2_overlay_label: string | null;
}

export interface MakerDiagnosticsWire {
  avg_markout_bps_label: string;       // "diagnostic_not_rank_axis"
  avg_toxicity_loss_bps_label: string; // "rank_axis_lower_is_better"
  real_executable_edge_bps_label: string; // "always_null_no_fill_or_pnl_claim"
}

// GET /maker/arena-result — the frozen `maker_arena_result.v1` envelope.
export interface MakerArenaResultResponseWire {
  schema_version: string; // "maker_arena_result.v1"
  lane: string;           // "maker"
  source_mode: string;    // "replay"
  rank_axis: string;      // "avg_toxicity_loss_bps"
  rank_axis_direction: string; // "asc"
  result: MakerArenaResultWire;
  proof_card: MakerProofCardWire;
  diagnostics: MakerDiagnosticsWire;
}
