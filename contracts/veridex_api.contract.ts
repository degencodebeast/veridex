// Frozen Veridex API contract (Plan A, Task 0). Generated/pinned by the backend; do not hand-edit
// field names without updating veridex/api/schemas.py + contracts/fixtures + tests/test_api_contract.py.

export type CheckStatus = "pass" | "fail" | "pending" | "not_applicable";
export type CheckSeverity = "blocking" | "warning" | "info";
export type CheckId =
  | "evidence_integrity" | "llm_boundary" | "metrics_recomputed" | "manifest_bound"
  | "policy_obeyed" | "receipt_separation" | "anchor";

export interface CheckResult {
  id: CheckId; label: string; result: CheckStatus; severity: CheckSeverity;
  method: string; scope: string; evidence_refs: string[]; rules: Record<string, unknown>[];
  details: Record<string, unknown>; error: string | null;
}

export interface ProofArtifact {
  verifier_version: string;
  run: Record<string, unknown>;
  lineage: Record<string, unknown>;
  evidence: { evidence_hash: string; run_event_count: number };
  checks: Record<CheckId, CheckResult>;   // SEC-001: CLV is NOT here
  anchor: { status: string; signature: string | null; cluster: string | null };
  metrics?: PerformanceMetrics | null;     // Performance Metrics block (CLV lives here)
}

export interface PerformanceMetrics {
  clv: number | null; sim_pnl: number | null; brier: number | null;
  max_drawdown: number | null; hit_rate: number | null; scored_actions: number;
  per_agent: Record<string, unknown>[];
}

export interface VerifyResult {
  run_id: string; verified: boolean; evidence_hash: string; recomputed_evidence_hash: string;
  manifest_hash: string; checks: Record<CheckId, CheckResult>; metrics: PerformanceMetrics | null;
  anchor: Record<string, unknown>; proof_card: ProofArtifact;
}

export interface LeaderboardRow {
  rank: number; agent_id: string; runs: number; avg_clv_bps: number | null; total_clv_bps: number;
  sim_pnl: number; brier: number | null; max_drawdown: number; action_count: number;
  valid_pct: number; proof_mode: string; eligibility_badge: string; anchor_status: string; source_mode: string;
}
export interface LeaderboardResponse { rows: LeaderboardRow[]; }

export interface CockpitState {           // GET /competitions/{id}
  competition_id: string; status: string; config: Record<string, unknown>;
  roster: Record<string, unknown>[];
  leaderboard: Record<string, unknown>[]; latest_seq: number; anchor_status: string;
  run_id: string | null; proof_card: ProofArtifact | null; execution: Record<string, unknown> | null;
}

export interface InspectorRecord {
  run_id: string; agent_id: string; tick_seq: number;
  market_state: Record<string, unknown>; agent_action: Record<string, unknown>;
  recompute: Record<string, unknown>; clv_bps: number | string;
  untrusted_llm_metadata: Record<string, unknown>;   // "NOT AN INPUT TO SCORE" (SEC-007)
}

export interface FeedHealth {
  source_mode: string; events_per_min: number | null; ws_live: boolean;
  last_tick_ts: number | null; anchor_status: string;
}

export type RuntimeEventType =
  | "run_started" | "status_changed" | "action_emitted" | "schema_validation"
  | "run_completed" | "run_failed" | "model_call_started" | "model_call_completed"
  | "token_usage" | "latency" | "tool_call" | "retry" | "error" | "trace_link";
export interface RuntimeEvent {
  type: RuntimeEventType; agent_id: string; run_id: string | null; session_id: string | null;
  ts: number; channel: "OPS"; payload: Record<string, unknown>;
}
export interface RuntimeEventsResponse { events: RuntimeEvent[]; }  // object wrapper; bind to .events
