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
