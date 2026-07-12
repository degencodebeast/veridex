// Frozen Veridex API contract (Plan A, Task 0). Generated/pinned by the backend; do not hand-edit
// field names without updating veridex/api/schemas.py + contracts/fixtures + tests/test_api_contract.py.
//
// SEC-001 MIGRATION NOTE: `checks` is pinned here to its FINAL target shape — the 7 CheckId only,
// with CLV/performance in `metrics` (NOT in `checks`). Consumers (C1/C2/D) bind to this final shape.
// The live backend completes this migration in Plan A, Task 5 (WD-5b): until then it still emits the
// legacy `clv`-in-`checks` block. A strict-xfail test in tests/test_api_contract.py asserts the SEC-001
// target against the live response and flips to a hard failure the moment Task 5 lands.

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
  checks: Record<CheckId, CheckResult>;   // SEC-001 target: the 7 CheckId only; CLV lives in `metrics` (see migration note)
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
  // WD-7 CLV confidence (display-only — NEVER a rank input, SEC-005):
  valid_count: number; clv_confidence: string; low_sample: boolean;
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

export interface FeedHealth {           // GET /feed/health — read-only telemetry (NOT scored)
  source_mode: string; events_per_min: number | null; ws_live: boolean;
  last_tick_ts: number | null; anchor_status: string;
  // WD-4 staleness view (additive — ws_live mirrors connected):
  txline_configured: boolean; connected: boolean; ticks_seen: number;
  fixture_id: number | null; staleness_s: number | null; stale: boolean;
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

// ---- MAKER LANE (maker_arena_result.v1) ----
// SEPARATE Maker*-prefixed family (SEC-005): MUST NOT reuse LeaderboardRow/ProofArtifact. The maker
// lane ranks on avg_toxicity_loss_bps (asc — lower is better), NOT any directional CLV.
// real_executable_edge_bps is ALWAYS null (no fill/PnL claim).
export interface MakerFalsificationWire {
  delta_bps: number; ci_low_bps: number; ci_high_bps: number; verdict: string; headline: string;
}
export interface MakerWindowClvAnalogWire {
  window_markout_bps: number; window_action_count: number; note: string;
}
export interface MakerLeaderboardRowWire {   // `maker_rank` (NOT `rank`); edge always null
  agent_id: string; avg_markout_bps: number; avg_toxicity_loss_bps: number;
  quote_count: number; scored: number; abstained: number;
  excluded: Record<string, unknown>; real_executable_edge_bps: null; maker_rank: number;
}
export interface MakerPerAgentWire {         // same fields, NO maker_rank
  agent_id: string; avg_markout_bps: number; avg_toxicity_loss_bps: number;
  quote_count: number; scored: number; abstained: number;
  excluded: Record<string, unknown>; real_executable_edge_bps: null;
}
export interface MakerArenaResultWire {
  protocol_id: string; config_hash: string; rung: string; fixtures: number[];
  per_agent: MakerPerAgentWire[]; maker_leaderboard: MakerLeaderboardRowWire[];
  falsification: MakerFalsificationWire;
  trade_aware_diagnostic: Record<string, unknown> | null;
  markout_adverse_decomposition: Record<string, unknown> | null;
  event_gate_timeline: Record<string, unknown> | null;
  window_clv_analog: MakerWindowClvAnalogWire;
  real_executable_edge_bps: null; fixture_universe_n: number; small_n_flag: boolean;
  excluded_by_reason: Record<string, unknown>; r2_bracket: Record<string, unknown> | null;
}
export interface MakerProofCardWire {
  rung: string; uncalibrated: boolean; headline: string;
  window_clv_analog: MakerWindowClvAnalogWire; falsification: MakerFalsificationWire;
  n_fixtures: number; small_n_note: string; trades_not_fills_caveat: string | null;
}
export interface MakerDiagnosticsWire {
  avg_markout_bps_label: string; avg_toxicity_loss_bps_label: string; real_executable_edge_bps_label: string;
}
export interface MakerArenaResultResponseWire {   // GET /maker/arena-result
  schema_version: string; lane: string; source_mode: string;
  rank_axis: string; rank_axis_direction: string;
  result: MakerArenaResultWire; proof_card: MakerProofCardWire; diagnostics: MakerDiagnosticsWire;
}
