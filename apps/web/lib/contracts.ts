// C1 frontend VIEW-MODEL types (assembled props the screens render). These are
// NOT the wire contract — lib/wire.ts mirrors the frozen contract, and lib/api.ts
// maps wire → view-model. The frontend binds to backend-computed results and NEVER
// reimplements law/scoring/checks (CON-003).
import type { CheckStatus } from '@/components/ui/ProofCheckChip';
import type { CheckId } from '@/lib/checks';

export type SourceMode = 'replay' | 'live';
export type ExecutionMode = 'paper' | 'dry_run' | 'live_guarded';
export type ProofMode = 'reproducible' | 'verified' | 'partial';
export type SportsActionType =
  | 'WAIT' | 'FLAG_VALUE' | 'FOLLOW_MOMENTUM' | 'FADE' | 'WIDEN_OR_SUSPEND';

export interface MarketQuote {
  stable_prob_bps: Record<string, number>;
  stable_price: Record<string, number>;
  suspended: boolean;
}
export interface MarketState {
  fixture_id: number;
  tick_seq: number;
  ts: number;
  phase: 0 | 1 | 2; // 0=pre, 1=in-running, 2=ended
  markets: Record<string, MarketQuote>;
  scores: Record<string, number>;
}

// reason/confidence/claimed_edge_bps are UNTRUSTED — never scored (SEC-007).
export interface AgentActionParams {
  market_key: string;
  side: string;
  reason: string;
  confidence: number;
  claimed_edge_bps: number;
}
export interface AgentAction {
  type: SportsActionType;
  params: AgentActionParams;
}

export interface ScoreRow {
  raw_prescore_hash: string;
  recomputed_edge_bps: number;
  agent_id: string;
  tick_seq: number;
  proof_mode: ProofMode;
  clv_bps: number;
  valid: boolean;
  reason: string;
  kelly_fraction: number;
  raw_prescore: number;
}

export interface RunEvent {
  sequence_no: number;
  event_type: string;
  state_snapshot_json: unknown;
  action_payload_json: unknown;
  validation_payload_json: unknown;
  result_payload_json: unknown;
}

export interface RunResult {
  run_id: string;
  source_mode: SourceMode;
  agent_ids: string[];
  run_events: RunEvent[];
  score_rows: ScoreRow[];
  evidence_hash: string;
  proof_mode_map: Record<string, ProofMode>;
}

export type AnchorStatus = 'anchored' | 'pending' | 'not_applicable' | 'not-anchored';
export interface LeaderboardRow {
  rank: number;
  agent_id: string;
  agent_name: string;
  agent_kind: string;
  runs: number;
  // The rank axis. null = UNSCORED (backend mean_clv_bps=None: no true-CLV scored actions) — render
  // "—" ("unavailable"), never a fabricated 0 bps (F-5). The backend still supplies the authoritative
  // rank, so a null-CLV agent keeps its server-assigned position without a fake number.
  avg_clv_bps: number | null;
  total_clv_bps: number;
  // These proxy metrics are carried by the CROSS-RUN wire LeaderboardRow but are ABSENT from the
  // competition-scoped CompetitionLeaderboardRow (F-5). null = honestly absent ("—"), never a fake 0.
  sim_pnl: number | null;
  brier: number | null;
  max_drawdown: number | null;
  action_count: number | null;
  valid_pct: number | null; // PERCENT 0-100 from the cross-run row; null on a competition-scoped row
  proof_mode: ProofMode;
  eligibility_badge: 'eligible' | 'not-eligible';
  anchor_status: AnchorStatus;
  source_mode: SourceMode | 'mixed';
  // WD-7 CLV confidence (display-only — NEVER a rank input, SEC-005). Mapped
  // faithfully from the wire LeaderboardRow in lib/api.ts.
  valid_count: number;
  clv_confidence: string;
  low_sample: boolean;
}

// ---- MAKER LANE view-model (maker_arena_result.v1) ----
// SEC-005: a SEPARATE, Maker-prefixed view-model — distinct from LeaderboardRow. The maker lane
// ranks on `avg_toxicity_loss_bps` (asc — lower is better), NOT any directional CLV. `maker_rank`
// (NOT `rank`) is the placement; `real_executable_edge_bps` is ALWAYS null (no fill/PnL claim).
export interface MakerLeaderboardRow {
  agent_id: string;
  maker_rank: number;            // NOT `rank` — maker-lane placement
  avg_toxicity_loss_bps: number; // THE rank axis (lower is better)
  avg_markout_bps: number;       // diagnostic, NOT a rank input
  quote_count: number;
  scored: number;
  abstained: number;
  real_executable_edge_bps: null; // always null (no fill/PnL claim)
}

export interface MakerFalsification {
  verdict: string;
  headline: string;
  delta_bps: number;
  ci_low_bps: number;
  ci_high_bps: number;
}

export interface MakerWindowClvAnalog {
  window_markout_bps: number;
  window_action_count: number;
  note: string;
}

export interface MakerProofCard {
  rung: string;
  uncalibrated: boolean;
  headline: string;
  n_fixtures: number;
  small_n_note: string;
  trades_not_fills_caveat: string | null;
  window_clv_analog: MakerWindowClvAnalog;
  falsification: MakerFalsification;
}

// AC-30/AC-31 (Gate-B REQ-026/027/052/053) — a displayed historical entry/exit capacity claim. It is
// ALWAYS a third-party print or a recomputed counterfactual ceiling: `capacity_usd` is BOUNDED by
// `matched_observed_liquidity_usd` and can NEVER render as our own fill / receipt / PnL / rank /
// Gate-B authority. Absent (undefined/null) on the sealed MM-R1 arena result — no such claim exists
// there — so the maker surfaces render exactly as before unless a claim is explicitly present.
export interface MakerCapacityClaim {
  kind: 'observed_market_print' | 'counterfactual';
  capacity_usd: number;                  // the raw wanted/printed size — a wish, not a fill
  matched_observed_liquidity_usd: number; // the observed ceiling the display is bounded DOWN to
}

// The assembled MAKER snapshot the (future) maker screen would render — Maker-prefixed, never routed
// through the taker/CLV LeaderboardRow adapter.
export interface MakerArenaResultView {
  schema_version: string;
  lane: string;
  source_mode: SourceMode;
  rank_axis: string;             // "avg_toxicity_loss_bps"
  rank_axis_direction: string;   // "asc"
  rung: string;
  // The sealed configuration identity (wire result.config_hash) — preserved verbatim so the
  // Maker Proof Card can show WHICH exact configuration produced this result (I-R M3).
  config_hash: string;
  fixture_universe_n: number;
  small_n_flag: boolean;
  real_executable_edge_bps: null; // top-level: always null
  leaderboard: MakerLeaderboardRow[];
  falsification: MakerFalsification;
  window_clv_analog: MakerWindowClvAnalog;
  proof_card: MakerProofCard;
  // The rank-axis honesty labels, carried verbatim (never a scored value).
  diagnostics: {
    avg_markout_bps_label: string;
    avg_toxicity_loss_bps_label: string;
    real_executable_edge_bps_label: string;
  };
  // AC-30/AC-31: an OPTIONAL historical entry/exit capacity claim. Always a bounded, LABELED
  // counterfactual — never our fill/PnL/rank. Null/absent on the sealed MM-R1 fixture.
  historical_capacity?: MakerCapacityClaim | null;
}

// ── QuoteGuard Behavior Ablation (F-8 · maker_live_ab.v1) ───────────────────────────────────────
// A read-only projection of the guard OFF vs ON behavior ablation: the SAME strategy on the SAME
// pinned tape with only the QuoteGuard arm flipped. It is a BEHAVIOR comparison, NOT a rank — the
// wire envelope deliberately carries NO rank / toxicity / CLV / PnL / edge / winner field, and this
// view-model mirrors it field-for-field (nothing invented, nothing scored).
export interface GuardAblationLeg {
  kind: string;
  role: string;
  price: number | null; // decimal odds when priced; null = honest "no price" (never coerced to 0)
  post_only: boolean;
}
export interface GuardAblationDecision {
  index: number; // the frame index this decision was taken on (timeline key)
  kind: string; // e.g. QUOTE | SUPPRESS — the decision the arm actually took
  reason_codes: string[]; // closed reason-code set the arm emitted (verbatim)
  legs: GuardAblationLeg[];
}
export interface GuardAblationArm {
  guard_enabled: boolean;
  terminal_reason: string | null; // why the arm stopped (e.g. tape_exhausted, guard_halt)
  observations_consumed: number;
  decisions: GuardAblationDecision[];
}
export interface GuardAblationView {
  schema_version: string; // "maker_live_ab.v1"
  lane: string; // "maker"
  panel: string; // "guard_on_off_ablation"
  is_ablation: boolean; // always true — a behavior ablation, never a ranking
  instance_id: string; // the maker instance the ablation was run for (echoed from the request)
  mode: string; // the replay/dry-run mode both arms ran under
  guard_off: GuardAblationArm;
  guard_on: GuardAblationArm;
  divergent_frame_indices: number[]; // frames where the two arms' substantive decision diverged
  diverges: boolean; // whether the guard flip changed the decision on at least one frame
  labels: Record<string, string>; // backend-provided honesty labels (ablation-not-ranking)
}

// NON-SCORING off-chain venue artifact (SEC-004).
export type ReceiptStatus =
  | 'proposed' | 'law_approved' | 'policy_approved' | 'submitted' | 'filled'
  | 'rejected' | 'cancelled';
export interface ExecutionReceipt {
  execution_id: string;
  venue: string;
  market_ref: string;
  side: string;
  requested_size: number;
  filled_size: number;
  price: number;
  status: ReceiptStatus;
  venue_order_id: string | null;
  mode: ExecutionMode;
  submitted_at: number | null;
  settled_at: number | null;
}

// One row of the cockpit's canonical event stream (seq · type · payload_hash · evidence?).
export interface CanonicalEvent {
  seq: number;
  type: string; // AGENT_ACTION | law_recomputed | score_update | policy_result | execution_receipt | proof_anchor | ...
  payload_hash: string;
  evidence: boolean; // true = sealed evidence prefix; false = derived non-scoring tail
  ts: number;
  agent_id?: string;
  summary?: string;
  // T10 live-projection extras — populated only for `law_result` wire events. A discriminated
  // union so a windowed value can NEVER be mistaken for true closing CLV (honesty doctrine):
  // 'clv' = true closing CLV, 'window_clv' = the run-window's close (NOT true CLV), 'pending' =
  // too little runway to score yet (an honest abstention, never a fabricated number).
  clv?: { kind: 'clv' | 'window_clv'; bps: number } | { kind: 'pending' };
  // Populated only for `policy_result` wire events — lets useArenaStream also push this decision
  // onto CockpitState.policy so the PolicyDecisions panel updates live.
  policy?: PolicyDecision;
}

export interface PolicyDecision {
  tick_seq: number;
  decision: 'ALLOW' | 'DENY' | 'REFUSE';
  reason: string;
  edge_bps?: number;
  min_edge_bps?: number;
  // DISPLAY-GATE signal (REQ-2D-501): the `edge_bps` value is the executable edge AT the venue
  // price — it renders ONLY when a REAL venue quote backs it (fail-closed). The min-edge THRESHOLD
  // is a config value and always renders. A Fake/paper quote never surfaces edge_bps as edge.
  real_venue_quote?: boolean;
}

// REQ-040 Match-State. NO `possession` field — not in the confirmed soccer stat set.
export type GamePhase = 'NS' | 'H1' | 'HT' | 'H2' | 'F';
export interface MatchState {
  fixture: string;
  phase: GamePhase;
  minute: number | null;
  goals: [number, number];
  yellow: [number, number];
  red: [number, number];
  corners: [number, number];
  status: 'scheduled' | 'live' | 'final';
  coverage?: string;
}

export interface RunHeaderState {
  fixture: string;
  competition: string;
  source_mode: SourceMode;
  execution_mode: ExecutionMode;
  proof_mode: ProofMode;
  events: number;
  valid_pct: number; // PERCENT 0-100 (matches the wire convention)
  verifier_version: string; // from the run's proof artifact (single source the status bar reads)
}

// GET /feed/health (WD-4) telemetry view-model — read-only, NEVER scored. `source_mode` is the
// honesty-gated data axis; `ws_live`/`connected`/`stale`/`staleness_s` are the real connection
// signals (the rail renders them verbatim — never a coerced "healthy/live" when the feed isn't).
export interface FeedHealthState {
  source_mode: SourceMode;
  ws_live: boolean;
  connected: boolean;
  txline_configured: boolean;
  events_per_min: number | null;
  ticks_seen: number;
  staleness_s: number | null;
  stale: boolean;
  fixture_id: number | null;
  anchor_status: AnchorStatus;
  last_tick_ts: number | null;
}

export type WsStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

export type ProofTraceStage =
  | 'evidence' | 'law' | 'policy' | 'receipt' | 'score' | 'anchor';
export interface ProofTraceItem {
  stage: ProofTraceStage;
  label: string;
  state: 'done' | 'active' | 'pending' | 'not_applicable';
}

// Assembled cockpit snapshot the route fetches and the screen renders (pure props).
export interface CockpitState {
  competition_id: string;
  run_id: string;
  header: RunHeaderState;
  trace: ProofTraceItem[];
  match: MatchState;
  leaderboard: LeaderboardRow[];
  events: CanonicalEvent[];
  receipts: ExecutionReceipt[];
  policy: PolicyDecision[];
  kill_armed: boolean;
}

// ---- Proof Card (REQ-020) ----
export interface CheckResult {
  id: CheckId;
  label: string;
  result: CheckStatus; // pass | fail | pending | not_applicable (SEC-002)
  severity: 'blocking' | 'warning' | 'info';
  method: string;
  scope: string;
  evidence_refs: string[];
  rules: { label: string; result: CheckStatus }[];
  details?: string;
  error?: string | null;
}

export interface PerformanceMetrics {
  clv_bps: number;
  sim_pnl: number; // proxy ⓟ
  brier: number; // proxy ⓟ
  hit_rate: number;
  max_drawdown: number;
}

// The 6th step ('policy') carries the policy/exec gate — justified by the real `policy`/`receipt`
// Merkle roots in the backend root-forest (veridex/chain/merkle.py build_root_forest).
export type ProofChainStepId = 'evidence' | 'pre-score' | 'policy' | 'score' | 'manifest' | 'anchor';
export interface ProofChainStep {
  id: ProofChainStepId;
  label: string;
  sub: string;
  hash: string;
  status: CheckStatus;
}

// One named Merkle root of the backend root-forest. `domain` is a REAL backend key (event_log /
// score / receipt / policy / competition / payout_reserved) — never invented. `root` is the hex.
export interface ProofRoot {
  domain: string;
  label: string;
  root: string;
}

export type ValidationMethod =
  | 'validateOdds' | 'validateFixture' | 'validateFixtureBatch' | 'validateStat';
export interface ValidationEntry {
  method: ValidationMethod;
  data_kind: 'odds' | 'fixture' | 'stat';
  message_id?: string;
  result: CheckStatus;
  root: string;
}

export interface AnchorInfo {
  status: AnchorStatus;
  tx_signature: string | null;
  cluster: string; // e.g. solana-devnet
  slot: number | null;
  committed_at: number | null;
  batching_note: string;
  explorer_url: string | null;
  manifest_hash?: string | null; // threaded from the verify result; honest-absent before a verify
}

export interface ProofArtifact {
  run_id: string;
  verifier_version: string;
  proof_mode: ProofMode;
  source_mode: SourceMode;
  evidence_hash: string;
  manifest_hash: string;
  run_event_count: number;
  schema_versions: Record<string, string>;
  chain: ProofChainStep[];
  checks: CheckResult[];
  metrics: PerformanceMetrics;
  validations: ValidationEntry[];
  anchor: AnchorInfo;
  proof_mode_map: Record<ProofMode, number>;
  // The named Merkle root-forest (6 real domains). Mapped from the served `lineage.root_forest`
  // when the backend serializes it; honest-empty ([]) until then (mock overlays a demo forest).
  roots: ProofRoot[];
}

// Returned by the AUTHORITATIVE backend verify/recompute endpoint (WD-1).
export interface VerifyResult {
  ok: boolean;
  verified: boolean; // wire `verified` preserved verbatim (trust-critical)
  evidence_hash_confirmed: boolean;
  manifest_hash_confirmed: boolean;
  recomputed: { recomputed_edge_bps: number; clv_bps: number; valid: boolean };
  manifest_hash: string; // raw manifest hash from the verify response (threaded to AnchorPanel/chain)
  anchor_tx: string | null;
  explorer_url: string | null;
  verifier_version: string;
  // The 7 trust checks + metrics from the authoritative recompute, preserved so
  // the VerifyButton can show per-check confirmation (SEC-001: no CLV in checks).
  checks: CheckResult[];
  metrics: PerformanceMetrics;
}

// ---- Decision Inspector (REQ-019) ----
export interface ClvExplanation {
  // Strategy-doctrine quantities (Task 22). The four decision inputs are nullable:
  // the wire InspectorRecord does NOT carry them yet, so `null` = "not in the proof
  // artifact" (rendered as "—"), distinct from a genuine computed 0 (honest-absence).
  fair_value_pct: number | null;          // de-margined consensus fair probability at entry
  closing_fair_value_pct: number | null;  // de-margined consensus fair probability at close
  venue_decimal_price: number | null;     // the actual venue decimal price
  // mispricing_gap = fair_prob_bps − venue_implied_prob_bps: a PROBABILITY-space dislocation.
  // Explanatory only — NEVER edge, NEVER scored (REQ-2D-501). Distinct from executable_edge (EV).
  mispricing_gap_bps: number | null;
  executable_edge_bps: number | null;     // forward EV at the venue price (NOT CLV, NOT the gap)
  // The DISPLAY-GATE honesty signal (REQ-2D-501 / AC-2D-501): true iff the venue price came from a
  // REAL venue quote (not the FakeVenueAdapter's fixed 2.05). Venue price / mispricing_gap /
  // executable_edge render ONLY when this is true — fail-closed. Live wire carries no quote → false.
  real_venue_quote: boolean;
  clv_bps: number;                         // the proven skill metric (the real scored value)
  clv_low_sample?: boolean;                // WD-7 sample-size flag — shown (never hidden), never a score
  stake_fraction: number | null;          // Kelly/policy sizing
  plain: string;
}
export interface UntrustedLlmMetadata {
  model: string;
  confidence: number;
  claimed_edge_bps: number;
  rationale: string;
}
export interface InspectorRecord {
  run_id: string;
  agent_id: string;
  action_seq: number;
  proof_mode: ProofMode;
  is_live: boolean;
  market_state: MarketState;
  agent_action: AgentAction;
  recompute: { recomputed_edge_bps: number; clv_bps: number; valid: boolean };
  clv_explanation: ClvExplanation;
  untrusted_llm: UntrustedLlmMetadata | null;
}
