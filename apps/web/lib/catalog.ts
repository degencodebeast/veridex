// C2 catalog/management contract types. REUSE-not-fork: shared shapes come from C1's
// lib/contracts.ts (view-model) + lib/wire.ts (frozen wire) and are RE-EXPORTED here for
// a single C2 import surface — never redefined. Only genuinely-C2-specific types are
// declared below. Logic is limited to the wire→view RuntimeEvent bridge (SEC-003 seam).
import type {
  SourceMode, ProofMode, ExecutionMode, AnchorStatus, ExecutionReceipt, LeaderboardRow,
  SportsActionType, FeedHealthState,
} from '@/lib/contracts';
import type { RuntimeEvent as WireRuntimeEvent, RuntimeEventType } from '@/lib/wire';

// Re-export the C1 shared types (no duplication; one import surface for C2 screens).
export type {
  SourceMode, ProofMode, ExecutionMode, AnchorStatus, ExecutionReceipt, LeaderboardRow,
  SportsActionType, FeedHealthState,
};

// SportsActionType const (C1 has the type only) — typed against C1's union so it can't drift.
export const SPORTS_ACTION_TYPES: readonly SportsActionType[] = [
  'WAIT', 'FLAG_VALUE', 'FOLLOW_MOMENTUM', 'FADE', 'WIDEN_OR_SUSPEND',
] as const;

export const ARCHETYPES = ['value_clv', 'baseline', 'momentum', 'contrarian', 'stale_line'] as const;
export type Archetype = (typeof ARCHETYPES)[number];

export const MARKET_FAMILY_KEYS = [
  '1X2_PARTICIPANT_RESULT', 'OVERUNDER_PARTICIPANT_GOALS', 'ASIANHANDICAP_PARTICIPANT_GOALS',
] as const;
export type MarketFamilyKey = (typeof MARKET_FAMILY_KEYS)[number];

// --- C2-specific enums (no C1 equivalent) ------------------------------------
export type EligibilityBadge = 'eligible' | 'not-eligible';
export type CompetitionType = 'live_arena' | 'replay_arena' | 'head_to_head' | 'prize_vault_challenge';
export type CompetitionLifecycle = 'upcoming' | 'live' | 'settled';
export type PayoutState =
  | 'pending' | 'design-target' | 'sponsor-funded' | 'manual approval' | 'paid' | 'failed' | '2D implementation';
export type LogChannel = 'OPS' | 'PROOF' | 'POLICY' | 'EXEC';
export type RuntimeStatus = 'running' | 'paused' | 'failed' | 'completed';
// Agents/runs can be mixed-source; C1's SourceMode is replay|live, so widen here.
export type CatalogSourceMode = SourceMode | 'mixed';

// --- Policy & competition -----------------------------------------------------
export interface PolicyEnvelope {
  max_stake: number;
  max_orders_per_run: number;
  max_orders_per_session: number;
  max_orders_per_day: number;
  venue_allowlist: string[];
  market_allowlist: string[];
  min_edge_bps: number;
  max_slippage_bps: number;
  max_price: number;
  max_quote_age_s: number;
  cooldown_s: number;
  human_approval_threshold: number;
  kill_switch: boolean;
}

// Studio PREFLIGHT PREVIEW view-model — FULLY (A) real config (codex option 3). policy_envelope +
// rule_config + min_edge_threshold_bps are all derived from the real config that WILL be pinned
// (SEC-009). There is intentionally NO computed/estimated pre-run edge value: pre-run edge is a
// per-run sealed proof quantity (same risk class as Markets EDGE), so Studio shows only the min-edge
// THRESHOLD ("Minimum executable edge ≥ N bps") + a plain disclaimer — never a proof-styled number.
export interface PreflightRule { field: string; value: string; }
export interface PreflightPreview {
  policy_envelope: PolicyEnvelope;
  rule_config: PreflightRule[];
  min_edge_threshold_bps: number; // the REAL policy threshold (config) — NOT a computed edge value
}

export interface CompetitionConfig {
  competition_type: CompetitionType;
  source_mode: SourceMode; // replay | live (never mixed for a single competition)
  execution_mode: ExecutionMode;
  market_scope: string;
  scoring_window: string;
  roster_size: number; // >= 2
  division: string;
  reward_policy: string;
  prize_vault_ref: string | null;
  operator_id: string;
  policy_envelope: PolicyEnvelope;
}

export interface CompetitionSummary {
  competition_id: string;
  title: string;
  competition_type: CompetitionType;
  lifecycle: CompetitionLifecycle;
  source_mode: SourceMode;
  execution_mode: ExecutionMode;
  proof_mode: ProofMode;
  market_scope: string;
  roster_size: number;
  events_per_min: number | null; // null while not live
  ws_live: boolean;
  settled_run_id: string | null; // for the Recent-Settled strip → Proof
  // Roadmappable DEMO field: a per-competition leader CLV shown ONLY in mock mode (no honest
  // comp→leader mapping yet). null ⇒ "—" even under mock (e.g. upcoming, no runs). Never LIVE.
  demo_leader_clv_bps?: number | null;
}

// --- Agents -------------------------------------------------------------------
export interface AgentSummary {
  agent_id: string;
  agent_name: string;
  archetype: Archetype;
  // A scored agent has a real strategy mode; the PUBLIC deployed-agent roster (GET /agents/roster)
  // carries no such label, so it is null there → the table renders "—" (honest, never fabricated).
  mode: 'llm' | 'numeric' | 'rule' | null;
  // Performance fields are null when there is no scoring aggregation for the row (e.g. every
  // /agents/roster row). null → the table renders "—"; NEVER fabricated as 0 (a real 0 is a claim).
  avg_clv_bps: number | null;
  runs: number | null;
  proof_mode: ProofMode;
  source_mode: CatalogSourceMode;
  valid_pct: number | null;
  source: 'STUDIO' | 'BYOA';
}

// Roster-local proof-state: ProofMode PLUS the honest states a PUBLIC deployed agent can carry that are
// NOT an earned single-mode proof claim: 'unscored' (deployed but no scored board rows yet), 'mixed'
// (the backend's honest cross-run aggregate when an agent's runs carry different proof modes —
// veridex/leaderboard.py:_summarize_proof_mode), and 'unknown' (fail-closed fallback for any proof_state
// string the frontend does not recognize — NEVER coerced to an unearned 'reproducible'). This
// deliberately does NOT widen the shared ProofMode (which has EXHAUSTIVE `Record<ProofMode, number>`
// uses in lib/contracts.ts + lib/api.ts) — 'unscored'/'mixed'/'unknown' live ONLY on this roster-local
// type, never on ProofMode.
export type ProofState = ProofMode | 'unscored' | 'mixed' | 'unknown';

// The PUBLIC deployed-agent roster row (GET /agents/roster) AND the shared identity contract the Duel
// surface (E4) consumes. Honest identity (public_agent_id / display_name / owner_public_label / origin)
// and honest proof_state ('unscored' until scored). Performance columns are null until the agent has
// scored board rows → render "—", NEVER fabricated. Carries NO STUDIO/BYOA `source` field.
export interface PublicAgentRow {
  public_agent_id: string;
  display_name: string;
  owner_public_label: string;
  origin: string;
  proof_state: ProofState;
  archetype: Archetype;
  mode: 'llm' | 'numeric' | 'rule' | null;
  avg_clv_bps: number | null;
  runs: number | null;
  valid_pct: number | null;
}

// The directional leaderboard row (GET /leaderboard/directional): the cross-run LeaderboardRow ENRICHED
// with honest public identity (display_name + public_agent_id) so the board renders REAL display names,
// never opaque ids. `extends LeaderboardRow` so a DirectionalRow[] is a valid LeaderboardRow[] for the
// existing LeaderboardScreen.
export interface DirectionalRow extends LeaderboardRow {
  display_name: string;
  public_agent_id: string;
  // The HONEST roster-local proof state for the board surface (Gate-3 M3). The shared
  // LeaderboardRow.proof_mode coerces the backend's cross-run aggregate "mixed" → 'reproducible'
  // (an unearned claim); this carries the honest 'mixed'/'unknown'/'unscored' so the board renders
  // the real aggregate. Never widens the shared ProofMode — the honesty lives ONLY here.
  proof_state: ProofState;
}

export interface AgentProfileRecord extends AgentSummary {
  valid_count: number;
  config_hash: string;
  policy_hash: string;
  strategy_caption: string; // generated, pinned to config_hash, never asserts performance
  completed_competitions: { competition_id: string; title: string; run_id: string; avg_clv_bps: number }[];
  anchors: { run_id: string; tx_signature: string; slot: number }[];
  deployment_provenance: string;
  total_clv_bps: number;
  eligibility_badge: EligibilityBadge;
  // OPTIONAL honesty flag for the leaner off-mock profile (no per-competition breakdown from the
  // directional board + roster). When false, an EMPTY completed_competitions list must render an
  // honest "not exposed" note instead of "No completed competitions yet." (the agent has runs>0, so
  // implying zero completed competitions would be dishonest). Absent/undefined on the mock
  // AGENT_PROFILES fixtures ⇒ they keep today's exact "none yet" copy.
  breakdown_available?: boolean;
}

// --- Operator dashboard slices ------------------------------------------------
export interface RunSummary {
  run_id: string;
  agent_id: string;
  agent_name: string;
  avg_clv_bps: number;
  proof_mode: ProofMode;
  anchor_status: AnchorStatus;
  source_mode: CatalogSourceMode;
}

export interface RewardSummary {
  competition_id: string;
  title: string;
  amount_label: string; // honest, e.g. "— (design target)"
  payout_state: PayoutState;
}

export interface OpsAlert {
  id: string;
  kind: 'kill' | 'deny' | 'hold';
  agent_id: string;
  message: string;
  ts: number;
}

// --- Runtime telemetry VIEW model (mapped from the frozen wire RuntimeEvent) ---
// The wire RuntimeEvent (lib/wire.ts) is the frozen OPS-telemetry shape; this is the
// drawer's display projection (runtime-neutral, SEC-010). OPS is telemetry, never proof.
export interface RuntimeEvent {
  kind: RuntimeEventType;
  ts: number;
  channel: LogChannel;
  summary: string;
}
export interface RuntimeEventsResponse { events: RuntimeEvent[] }

export interface RuntimeOverview {
  agent_id: string;
  run_id: string | null;
  status: RuntimeStatus;
  latest_model_latency_ms: number | null; // optional tier → null renders "—"
  latest_model_tokens: number | null;
  last_action: SportsActionType | null;
  schema_valid: boolean | null;
  errors: number;
  retries: number;
  tool_calls: number;
  source: 'STUDIO' | 'BYOA';
}

// A merged log line for the Logs tab. POLICY/EXEC are canonical but derived/non-scoring;
// OPS is runtime telemetry, never proof (SEC-003).
export interface CanonicalLogLine {
  ts: string;
  channel: LogChannel;
  event: string;
  detail: string;
}

// --- TxLINE market data (spec §4.5) ------------------------------------------
export interface OddsUpdate {
  fixture_id: number;
  message_id: string;
  ts: number;
  in_running: boolean;
  market_family: MarketFamilyKey;
  market_parameters: string | null; // e.g. "line=2.5"
  price_names: string[];
  prices: number[]; // int, decimal x1000
  pct: string[];    // implied %, 3dp string
}

export interface MarketFamily {
  key: MarketFamilyKey;
  label: string;
  rows: {
    parameters: string | null;
    outcomes: { name: string; decimal: number; impliedPct: string; closing: number | null }[];
  }[];
}

export interface FixtureSummary {
  fixture_id: number;
  pack_id: string;
  competition: string;
  participant1: string;
  participant2: string;
  start_time: string;
  in_running: boolean;
}

export interface SportNode {
  id: string;
  label: string; // includes the emoji glyph, e.g. "⚽ Soccer"
  enabled: boolean;
  disabledReason?: string; // "not in free feed / coming soon"
  competitions: { id: string; label: string; enabled: boolean }[];
}

// --- Response wrappers (reuse C1 LeaderboardRow / ExecutionReceipt) -----------
export interface LeaderboardResponse { rows: LeaderboardRow[] }

export interface CompetitionStateResponse {
  status: CompetitionLifecycle;
  config: CompetitionConfig;
  roster: AgentSummary[];
  leaderboard: LeaderboardRow[];
  latest_seq: number;
  anchor_status: AnchorStatus;
  run_id: string | null;
  proof_card: string | null; // proof-card ref (C1 renders it; C2 only links)
  execution: ExecutionReceipt[];
}

// --- wire → view RuntimeEvent bridge + the canonical-only seam (SEC-003) ------
// The Ops drawer's default filter HIDES OPS telemetry (invariant #1) and OPS is never
// scored/ranked (invariant #2): only PROOF/POLICY/EXEC are canonical channels.
export function isCanonicalChannel(channel: LogChannel): boolean {
  return channel !== 'OPS';
}

export function toViewRuntimeEvent(e: WireRuntimeEvent): RuntimeEvent {
  const summary = typeof e.payload.summary === 'string' ? e.payload.summary : JSON.stringify(e.payload);
  return { kind: e.type, ts: e.ts, channel: e.channel, summary };
}
