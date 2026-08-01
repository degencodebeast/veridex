// Typed read/control client. Binds the FROZEN wire contract (lib/wire.ts, mirror
// of contracts/veridex_api.contract.ts) and maps it to the screen view-model
// (lib/contracts.ts). The frontend NEVER reimplements law/scoring/checks (CON-003);
// verify is an authoritative backend recompute (WD-1) — this client only relays it.
//
// Trust-critical mappings are exhaustively unit-tested (lib/api.test.ts):
//   - proof: all 7 lowercase checks preserved with their real statuses, no CLV in checks
//   - leaderboard: WD-7 confidence fields (valid_count/clv_confidence/low_sample) preserved
//   - verify: wire `verified` + the 7 checks preserved onto the view-model
//
// REPRESENTATIONAL GAPS (view-model fields the frozen contract does NOT carry — see
// the report; the screens fill these from other sources or they stay defaulted):
//   - ProofArtifact: manifest_hash, chain[], validations[] are not in wire ProofArtifact.
//   - CockpitState: trace/match/events/receipts/policy/kill_armed/rich-leaderboard are
//     not in wire CompetitionStateResponse; they come from the WS stream + /leaderboard.
//   - InspectorRecord: proof_mode/is_live/clv_explanation are not in wire InspectorRecord.
import { CHECK_ORDER } from '@/lib/checks';
import { getAuthToken } from '@/lib/auth';
import { isMockEnabled, MOCK_FIXTURES } from '@/lib/mock';
import { COCKPIT_DEMO } from '@/lib/fixtures/cockpit';
import { INSPECTOR_DEMO_QUANTITIES } from '@/lib/fixtures/inspector';
import { PROOF_DEMO_ROOTS, mapRootForest } from '@/lib/fixtures/proof';
import { PROOF_EXPLAIN_DEMO, type ProofExplanation } from '@/lib/explainer';
import { VERIFIER_VERSION, type StatusBarState } from '@/lib/status';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';
import type { AgentProfileRecord, Archetype, DirectionalRow, FixtureSummary, MarketFamilyKey, OddsUpdate, ProofState, PublicAgentRow } from '@/lib/catalog';
import type * as W from '@/lib/wire';
import type {
  AnchorInfo, AnchorStatus, CheckResult, CockpitState, ExecutionMode, FeedHealthState,
  GuardAblationArm, GuardAblationDecision, GuardAblationLeg, GuardAblationView,
  ExecutionReceipt, InspectorRecord, LeaderboardRow, MakerArenaResultView, MakerLeaderboardRow, MatchState,
  PerformanceMetrics, ProofArtifact, ProofMode, ReceiptStatus, SourceMode, VerifyResult,
} from '@/lib/contracts';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '';

// Resolve a request URL against the configured API base. The base is read at CALL TIME (Next inlines
// NEXT_PUBLIC_* into the client bundle, and it is a live env read on the server), so SSR fetches
// resolve to an ABSOLUTE URL. Fail-closed on the server: a missing base is a boot error because a
// relative URL cannot be fetched in Node — never silently hit the wrong origin. In the browser a
// same-origin relative path is correct, so an unset base falls through to the bare path.
function resolveApiUrl(path: string): string {
  const base = process.env.NEXT_PUBLIC_API_BASE ?? '';
  if (base) return `${base}${path}`;
  if (typeof window === 'undefined') {
    throw new Error(
      'NEXT_PUBLIC_API_BASE is required for server-side rendering: set it to the absolute API origin ' +
        '(e.g. https://api.example.com). A relative URL cannot be fetched during SSR.',
    );
  }
  return path;
}

// The directional board_kind is a CLOSED wire enum whose values EXACTLY match the backend
// (veridex/public_projection.py:108-117 — LOWERCASE 'official_benchmark' | 'public_agents'). An
// UPPERCASE value is REJECTED with 422, so the frontend must never send one (probe: PUBLIC_AGENTS → 422,
// public_agents → 200). This type pins the wire values so a stray uppercase literal cannot compile.
export type BoardKindWire = 'official_benchmark' | 'public_agents';

// Centralized path map — the C1 binding points. A route change is a one-line edit.
export const PATHS = {
  // The backend serves the ProofArtifact at GET /runs/{id} (no /proof suffix — pinned by
  // tests/test_api_contract.py; /api/proof is 404). A regression re-adding /proof false-404s the card.
  runProof: (runId: string) => `/runs/${runId}`,
  verify: (runId: string) => `/runs/${runId}/verify`,
  explain: (runId: string) => `/runs/${runId}/explain`,
  competitionState: (id: string) => `/competitions/${id}`,
  competitionEvents: (id: string, sinceSeq = 0) => `/competitions/${id}/events?since_seq=${sinceSeq}`,
  leaderboard: (competitionId?: string) =>
    competitionId ? `/leaderboard?competition_id=${competitionId}` : `/leaderboard`,
  inspector: (runId: string, seq: number | string) => `/runs/${runId}/actions/${seq}`,
  feedHealth: () => `/feed/health`,
  // I-4 OWNER-SCOPED runtime-events (OPS telemetry). REPLACES the retired PUBLIC
  // `/agents/{id}/runtime-events`: ownership is resolved server-side from the persisted instance
  // (router.py get_instance_runtime_events). Cursor-polling: `since` is an EXCLUSIVE durable `id`
  // cursor (0 ⇒ from the start); `limit` forward-pages the first N events after `since`.
  instanceRuntimeEvents: (instanceId: string, since = 0, limit?: number) =>
    `/agents/instances/${instanceId}/runtime-events?since=${since}` + (limit != null ? `&limit=${limit}` : ''),
  // MAKER lane: the sealed maker_arena_result.v1 envelope (quote-quality, toxicity-ranked).
  makerArenaResult: () => `/maker/arena-result`,
  // F-8 QuoteGuard behavior ablation (maker_live_ab.v1). Read-only, public, per-instance. 404s
  // (honest "no ablation for this instance") until an ablation provider is wired for the instance.
  makerLiveAb: (instanceId: string) => `/maker/live-ab/${encodeURIComponent(instanceId)}`,
  // R-3 verified ReplayPack catalog (read-only). Enriched with additive fixture_metadata.
  replayPacks: () => `/replay-packs`,
  // E2 read-only replay-market projection: one fixture's LAST-KNOWN odds per market (folded across
  // the whole hash-bound tape). pack_id is a catalog KEY resolved server-side, never a filesystem path.
  replayMarkets: (packId: string, fixtureId: number) =>
    `/replay-packs/${encodeURIComponent(packId)}/fixtures/${fixtureId}/markets`,
  // PUBLIC deployed-agent roster (read-only, unauth — mirrors /replay-packs). ALL owners, NOT
  // owner-filtered (distinct from the owner-scoped /agents/instances). Perf columns are honest null.
  agentsRoster: () => `/agents/roster`,
  // B3 directional leaderboard completion layer (read-only). `board_kind` is a CLOSED backend enum
  // (LOWERCASE 'official_benchmark' / 'public_agents' — an uppercase value 422s); the server
  // visibility-joins + aggregates. Rows are enriched with honest public identity (display_name +
  // public_agent_id).
  directionalLeaderboard: (boardKind: BoardKindWire) => `/leaderboard/directional?board_kind=${boardKind}`,
  // F-4 competition lifecycle (owner-scoped POSTs): create → register roster entry → start.
  competitions: () => `/competitions`,
  competitionAgents: (id: string) => `/competitions/${id}/agents`,
  competitionStart: (id: string) => `/competitions/${id}/start`,
  // I-2 owner-scoped deployed instances. Bearer-authed GETs; the backend 401s anon
  // (require_principal), returns ONLY the caller's own rows, and 403/404s a non-owner.
  agentInstances: () => `/agents/instances`,
  agentInstance: (instanceId: string) => `/agents/instances/${instanceId}`,
  // F-7 owner-scoped instance LIFECYCLE. Status is a read-only run/lease view (deploy.py:985); kill
  // is the owner-gated exactly-once shutdown-cancel (deploy.py:1008). Both 401 anon / 403 non-owner /
  // 404 absent before any effect. Kill additionally 409s a run that was never minted / is not live.
  instanceStatus: (instanceId: string) => `/agents/instances/${instanceId}/status`,
  instanceKill: (instanceId: string) => `/agents/instances/${instanceId}/kill`,
  // F-7 "Disable execution" maps to the competition policy-envelope kill-switch (router.py:1654):
  // ENGAGE-ONLY + idempotent (SAF-004) — it SETS the stop True and can never re-open trading.
  competitionKillSwitch: (competitionId: string) => `/competitions/${competitionId}/kill-switch`,
} as const;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

// POST an owner-scoped route with the auth-contract@1 bearer. Attaches Authorization when the
// seam (lib/auth.ts) has a token; omits it otherwise — the fail-closed "no token ⇒ never fires"
// guarantee lives at the UI layer (components/auth/AuthGate keeps the gated affordance out of the
// DOM entirely) plus the backend, which 401s any request missing a valid bearer before any side
// effect (auth-contract@1). This client stays a dumb, honest transport: it never fabricates a
// bearer and never silently eats a 401. On a 401 it re-acquires the token (re-auth) and retries
// EXACTLY ONCE — never an infinite loop; the final response is returned as-is for the caller to
// inspect, so a persistent 401 always surfaces (via the existing !res.ok → ApiError check).
// Shared owner-scoped transport core: acquire the seam's bearer, fire the request, and on a 401
// re-acquire the token (re-auth) and retry EXACTLY ONCE. Both the POST (deploy) and the GET
// (owner-scoped reads) helpers below share this identical bearer + 401-retry contract, so the
// honesty posture (never fabricate a bearer; never silently eat a 401) lives in one place.
async function authedRequest(path: string, buildInit: (bearer: string | null) => RequestInit): Promise<Response> {
  const token = await getAuthToken();
  const doFetch = (bearer: string | null) => fetch(resolveApiUrl(path), buildInit(bearer));
  let res = await doFetch(token);
  if (res.status === 401) {
    const retryToken = await getAuthToken(); // re-auth: re-acquire token / re-login
    if (retryToken) res = await doFetch(retryToken);
  }
  return res;
}

async function authedFetch(
  path: string,
  body: unknown,
  extraHeaders?: Record<string, string>,
): Promise<Response> {
  return authedRequest(path, (bearer) => ({
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      accept: 'application/json',
      ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
      // Caller-supplied request headers (e.g. the deploy Idempotency-Key). Spread LAST so an
      // explicit caller header is honest and visible — the transport never fabricates one.
      ...(extraHeaders ?? {}),
    },
    body: JSON.stringify(body),
  }));
}

// Owner-scoped authed GET (auth-contract@1): same bearer + 401-retry as authedFetch, no body.
// A no-token call fires WITHOUT an Authorization header — it never fabricates a bearer; the
// backend then 401s (require_principal) before returning any owner-private data (fail-closed).
async function authedGet(path: string): Promise<Response> {
  return authedRequest(path, (bearer) => ({
    method: 'GET',
    headers: {
      accept: 'application/json',
      ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    },
  }));
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(resolveApiUrl(path), { headers: { accept: 'application/json' } });
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(resolveApiUrl(path), {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, `POST ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

// ---- pure coercion helpers ----
// Anchor status is BACKEND-AUTHORITATIVE (v2.10.10 §A6): render the backend's word verbatim, never
// a fabricated anchor. Two backend vocabularies feed this one coercion: the canonical per-surface
// vocabulary ("anchored" / "not_anchored" / "pending" / "not_applicable" — proof/feed/competition,
// veridex/api/schemas.py:96,308) AND the cross-run leaderboard AGGREGATE vocabulary ("all-anchored" /
// "some-pending" / "none-anchored" — schemas.py:53, veridex/leaderboard.py:66-82). Every "not / absent
// / unverified / unknown" word FAILS CLOSED to `not-anchored` — an absent anchor is NEVER a green one.
function toAnchorStatus(s: string): AnchorStatus {
  if (s === 'anchored' || s === 'all-anchored') return 'anchored';
  if (s === 'pending' || s === 'some-pending') return 'pending';
  if (s === 'not_applicable') return 'not_applicable';
  return 'not-anchored'; // not_anchored | none-anchored | absent | unknown ⇒ never a fabricated anchor
}
function toProofMode(s: string): ProofMode {
  return s === 'verified' || s === 'partial' ? s : 'reproducible';
}
function toSourceMode(s: string): SourceMode {
  return s === 'live' ? 'live' : 'replay';
}
function toExecutionMode(s: string): ExecutionMode {
  return s === 'dry_run' || s === 'live_guarded' ? s : 'paper';
}
// The backend ExecutionStatus enum (veridex/execution/models.py) is a SUPERSET of the frontend
// ReceiptStatus lane. Map the seven lane values 1:1; fold the extras onto the CLOSEST lane stage
// that never OVERSTATES progress (accepted/partial → submitted, settled → filled), route awaiting_human
// to policy_approved, and treat expired/voided/unresolved as a terminal-negative (cancelled). Unknown
// ⇒ the most conservative 'proposed' — never a fabricated later stage.
function toReceiptStatus(s: string): ReceiptStatus {
  switch (s) {
    case 'proposed': case 'law_approved': case 'policy_approved':
    case 'submitted': case 'filled': case 'rejected': case 'cancelled':
      return s;
    case 'accepted': case 'partial': return 'submitted';
    case 'settled': return 'filled';
    // awaiting_human is BLOCKED waiting for a human, strictly BEFORE policy_approved (models.py
    // transition graph: law_approved → awaiting_human → policy_approved). Render its last provably-
    // reached state, law_approved — NEVER policy_approved, which would claim an approval not yet given.
    case 'awaiting_human': return 'law_approved';
    case 'expired': case 'voided': case 'unresolved': return 'cancelled';
    default: return 'proposed';
  }
}
// Receipt timestamps arrive as ISO strings on the wire (backend ExecutionReceipt.submitted_at: str|None);
// the view-model carries epoch seconds. Preserve null verbatim (honest "not yet"), parse an ISO string
// faithfully, and fail an unparseable value to null — understating, never fabricating, progress.
function toEpochSeconds(v: unknown): number | null {
  if (v == null) return null;
  if (typeof v === 'number') return v;
  const ms = Date.parse(String(v));
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

// wire rules are arbitrary records (e.g. {all_metrics_match: true}); represent each
// as a label/result pair for display (the trust signal is the parent check result).
function mapRules(rules: Record<string, unknown>[]): { label: string; result: CheckResult['result'] }[] {
  return rules.map((r) => {
    const [label, value] = Object.entries(r)[0] ?? ['rule', null];
    return { label, result: value === true ? 'pass' : value === false ? 'fail' : 'not_applicable' };
  });
}

function mapCheck(w: W.CheckResult): CheckResult {
  return {
    id: w.id, // already lowercase, matches CheckId
    label: w.label,
    result: w.result, // preserved verbatim — never coerced (SEC-002)
    severity: w.severity,
    method: w.method,
    scope: w.scope,
    evidence_refs: w.evidence_refs,
    rules: mapRules(w.rules),
    details: Object.keys(w.details).length ? JSON.stringify(w.details) : undefined,
    error: w.error,
  };
}

function mapMetrics(m: W.PerformanceMetrics | null | undefined): PerformanceMetrics {
  return {
    clv_bps: m?.clv ?? 0,
    sim_pnl: m?.sim_pnl ?? 0,
    brier: m?.brier ?? 0,
    hit_rate: m?.hit_rate ?? 0,
    max_drawdown: m?.max_drawdown ?? 0,
  };
}

function mapAnchor(a: { status: string; signature: string | null; cluster: string | null }): AnchorInfo {
  return {
    status: toAnchorStatus(a.status),
    tx_signature: a.signature,
    cluster: a.cluster ?? 'solana-devnet',
    slot: null, // GAP: not in wire anchor
    committed_at: null, // GAP
    batching_note: '',
    explorer_url: null,
  };
}

function countModes(map: Record<string, unknown>): Record<ProofMode, number> {
  const out: Record<ProofMode, number> = { reproducible: 0, verified: 0, partial: 0 };
  for (const v of Object.values(map)) {
    if (v === 'reproducible' || v === 'verified' || v === 'partial') out[v] += 1;
  }
  return out;
}

// ---- wire → view-model adapters (exported for unit tests) ----
export function adaptProofArtifact(w: W.ProofArtifact): ProofArtifact {
  const run = w.run as { run_id?: string; source_mode?: string };
  const lineage = w.lineage as { proof_mode_map?: Record<string, unknown>; schema_versions?: Record<string, string> };
  const modes = countModes(lineage.proof_mode_map ?? {});
  const representativeMode: ProofMode = modes.verified ? 'verified' : modes.partial ? 'partial' : 'reproducible';
  return {
    run_id: String(run.run_id ?? ''),
    verifier_version: w.verifier_version,
    proof_mode: representativeMode,
    source_mode: toSourceMode(String(run.source_mode ?? 'replay')),
    evidence_hash: w.evidence.evidence_hash,
    manifest_hash: '', // GAP: not in wire ProofArtifact (only in VerifyResult)
    run_event_count: w.evidence.run_event_count,
    schema_versions: lineage.schema_versions ?? {},
    chain: [], // GAP: proof-chain steps not in wire ProofArtifact
    checks: CHECK_ORDER.map((id) => mapCheck(w.checks[id])), // all 7, in order, statuses preserved
    metrics: mapMetrics(w.metrics),
    validations: [], // GAP: on-chain validation entries not in wire ProofArtifact
    anchor: mapAnchor(w.anchor),
    proof_mode_map: modes,
    // Maps the served root_forest (6 real named roots) when present; honest-empty [] until then.
    roots: mapRootForest(w.lineage),
  };
}

export function adaptVerify(w: W.VerifyResult): VerifyResult {
  return {
    ok: w.verified,
    verified: w.verified,
    evidence_hash_confirmed: w.evidence_hash === w.recomputed_evidence_hash,
    manifest_hash_confirmed: w.checks.manifest_bound?.result === 'pass',
    recomputed: {
      // GAP: wire verify is run-level; no per-action edge. Map CLV through; edge mirrors it.
      recomputed_edge_bps: w.metrics?.clv ?? 0,
      clv_bps: w.metrics?.clv ?? 0,
      valid: w.verified,
    },
    manifest_hash: w.manifest_hash, // PRESERVE the raw manifest hash (threaded to AnchorPanel/chain)
    anchor_tx: (w.anchor as { signature?: string | null }).signature ?? null,
    explorer_url: null,
    verifier_version: w.proof_card.verifier_version,
    checks: CHECK_ORDER.map((id) => mapCheck(w.checks[id])), // preserved (SEC-001: no CLV)
    metrics: mapMetrics(w.metrics),
  };
}

export function adaptLeaderboard(w: W.LeaderboardResponse): LeaderboardRow[] {
  return w.rows.map((r) => ({
    rank: r.rank,
    agent_id: r.agent_id,
    agent_name: r.agent_id, // GAP: wire has no agent_name — fall back to id
    agent_kind: '', // GAP: wire has no agent_kind
    runs: r.runs,
    // avg_clv_bps is the rank axis; the wire carries `number | null` (backend `float | None`, None
    // when action_count == 0 — an UNSCORED agent). Preserve null verbatim ⇒ "—", NEVER a fabricated
    // 0 bps (R-globalclv — mirrors the F-5 competition-board fix on this cross-run/global surface).
    avg_clv_bps: r.avg_clv_bps,
    total_clv_bps: r.total_clv_bps,
    sim_pnl: r.sim_pnl,
    brier: r.brier ?? 0,
    max_drawdown: r.max_drawdown,
    action_count: r.action_count,
    valid_pct: r.valid_pct, // PERCENT 0-100, passed through 1:1 from the wire
    proof_mode: toProofMode(r.proof_mode),
    // eligibility is BACKEND-AUTHORITATIVE (anchor-derived server-side: fully-proven iff every run is
    // anchored — veridex/leaderboard.py:_eligibility_badge). Read the backend `eligibility_badge`
    // ("fully-proven" | "partially-proven" | "unproven", schemas.py:52) — NEVER re-derive from
    // proof_mode. Only a fully-proven agent is eligible; partial/unproven fail closed to not-eligible.
    eligibility_badge: r.eligibility_badge === 'eligible' || r.eligibility_badge === 'fully-proven' ? 'eligible' : 'not-eligible',
    anchor_status: toAnchorStatus(r.anchor_status),
    // source_mode is the backend cross-run AGGREGATE ("all-replay" | "all-live" | "mixed" | "unknown"
    // — schemas.py:54, veridex/leaderboard.py:_summarize_source_mode). An all-replay board renders
    // `replay` (never a spurious `mixed`); all-live renders `live`; anything else is honestly `mixed`.
    // Plain "replay"/"live" are also accepted so single-run/fixture rows keep mapping 1:1.
    source_mode:
      r.source_mode === 'live' || r.source_mode === 'all-live' ? 'live'
      : r.source_mode === 'replay' || r.source_mode === 'all-replay' ? 'replay'
      : 'mixed',
    // WD-7 confidence preserved faithfully (display-only, never reorders — SEC-005).
    valid_count: r.valid_count,
    clv_confidence: r.clv_confidence,
    low_sample: r.low_sample,
  }));
}

// The competition-scoped leaderboard row (backend CompetitionLeaderboardRow, GET /competitions/{id}).
// SMALLER than the cross-run wire LeaderboardRow: it carries ONLY the CLV rank axis + identity, no
// per-agent sim_pnl/brier/max_drawdown/action_count/valid_pct/agent_name.
interface CompetitionLeaderboardRowWire {
  rank: number;
  agent_id: string;
  total_clv_bps: number;
  mean_clv_bps: number | null;
  valid_count: number;
  proof_mode: string | null;
}

// F-5: bind the BACKEND-AUTHORITATIVE competition leaderboard into the cockpit view-model. The rows
// arrive already ranked + ordered by the server (CON-203, ranked by mean_clv_bps desc) — this maps
// them 1:1 and NEVER re-sorts (a client CLV re-sort would silently disagree with the sealed board).
// Metrics the competition contract does not carry are null (honest "—"), never a fabricated 0.
export function adaptCompetitionLeaderboard(
  rows: CompetitionLeaderboardRowWire[], sourceMode: SourceMode, anchorStatus: AnchorStatus,
): LeaderboardRow[] {
  return rows.map((r) => {
    const proof_mode = toProofMode(r.proof_mode ?? '');
    return {
      rank: r.rank,                         // backend rank, verbatim
      agent_id: r.agent_id,
      agent_name: r.agent_id,               // GAP: competition row has no display name — fall back to id
      agent_kind: '',                       // GAP: not in the competition contract
      runs: 0,                              // GAP: single sealed run, not a cross-run count
      avg_clv_bps: r.mean_clv_bps,          // mean_clv_bps → avg_clv_bps; null (UNSCORED) preserved ⇒ "—", never a fake 0
      total_clv_bps: r.total_clv_bps,
      sim_pnl: null, brier: null, max_drawdown: null, action_count: null, valid_pct: null, // ABSENT ⇒ "—"
      proof_mode,
      // II-W defect 5: the competition-scoped CompetitionLeaderboardRow (veridex/api/schemas.py:152-172)
      // carries NO authoritative eligibility field — only {rank, agent_id, total/mean_clv_bps,
      // valid_count, proof_mode}. Re-deriving eligibility from proof_mode violates the "UI never
      // re-derives" contract, so FAIL CLOSED to not-eligible — never a fabricated "eligible" without an
      // authoritative signal. (This surface is not rendered as an eligibility column today; ClvLeaderboard
      // shows proof_mode + anchor only. A true per-agent competition eligibility field is a backend follow-up.)
      eligibility_badge: 'not-eligible',
      anchor_status: anchorStatus,          // competition-level anchor status, applied verbatim
      source_mode: sourceMode,
      valid_count: r.valid_count,           // real count from the competition contract
      clv_confidence: '',                   // GAP: not classified per-agent in the competition contract
      low_sample: false,                    // GAP: not classified — display-only, never a rank input (SEC-005)
    };
  });
}

// F-5: project the sealed decision receipts from the NON-SCORING execution attachment (REQ-2B-20 —
// off-chain venue artifact, separate from the Phase-1 Memo anchor), field-for-field. Empty when the
// competition has no execution records (honest-empty, never a fixture).
export function adaptExecutionReceipts(execution: Record<string, unknown> | null | undefined): ExecutionReceipt[] {
  const raw = (execution?.receipts as Array<Record<string, unknown>> | undefined) ?? [];
  return raw.map((r) => ({
    execution_id: String(r.execution_id ?? ''),
    venue: String(r.venue ?? ''),
    market_ref: String(r.market_ref ?? ''),
    side: String(r.side ?? ''),
    requested_size: Number(r.requested_size ?? 0),
    filled_size: Number(r.filled_size ?? 0),
    price: Number(r.price ?? 0),
    status: toReceiptStatus(String(r.status ?? '')),
    venue_order_id: r.venue_order_id == null ? null : String(r.venue_order_id),
    mode: toExecutionMode(String(r.mode ?? 'paper')),
    submitted_at: toEpochSeconds(r.submitted_at),
    settled_at: toEpochSeconds(r.settled_at),
  }));
}

// MAKER lane adapter (SEC-005): a SEPARATE path — it does NOT reuse adaptLeaderboard /
// adaptProofArtifact and never touches any directional CLV type. The maker lane ranks on
// `avg_toxicity_loss_bps` (asc — lower is better). `real_executable_edge_bps` is carried through as
// the honest `null` (no fill/PnL claim), never coerced to 0.
export function adaptMakerArenaResult(w: W.MakerArenaResultResponseWire): MakerArenaResultView {
  const rows: MakerLeaderboardRow[] = w.result.maker_leaderboard.map((r) => ({
    agent_id: r.agent_id,
    maker_rank: r.maker_rank, // NOT `rank` — the maker-lane placement
    avg_toxicity_loss_bps: r.avg_toxicity_loss_bps,
    avg_markout_bps: r.avg_markout_bps,
    quote_count: r.quote_count,
    scored: r.scored,
    abstained: r.abstained,
    real_executable_edge_bps: null, // always null — no fill/PnL claim
  }));
  return {
    schema_version: w.schema_version,
    lane: w.lane,
    source_mode: toSourceMode(w.source_mode),
    rank_axis: w.rank_axis,
    rank_axis_direction: w.rank_axis_direction,
    rung: w.result.rung,
    config_hash: w.result.config_hash, // sealed configuration identity, verbatim (I-R M3)
    fixture_universe_n: w.result.fixture_universe_n,
    small_n_flag: w.result.small_n_flag,
    real_executable_edge_bps: null,
    leaderboard: rows,
    falsification: {
      verdict: w.result.falsification.verdict,
      headline: w.result.falsification.headline,
      delta_bps: w.result.falsification.delta_bps,
      ci_low_bps: w.result.falsification.ci_low_bps,
      ci_high_bps: w.result.falsification.ci_high_bps,
    },
    window_clv_analog: {
      window_markout_bps: w.result.window_clv_analog.window_markout_bps,
      window_action_count: w.result.window_clv_analog.window_action_count,
      note: w.result.window_clv_analog.note,
    },
    proof_card: {
      rung: w.proof_card.rung,
      uncalibrated: w.proof_card.uncalibrated,
      headline: w.proof_card.headline,
      n_fixtures: w.proof_card.n_fixtures,
      small_n_note: w.proof_card.small_n_note,
      trades_not_fills_caveat: w.proof_card.trades_not_fills_caveat,
      window_clv_analog: {
        window_markout_bps: w.proof_card.window_clv_analog.window_markout_bps,
        window_action_count: w.proof_card.window_clv_analog.window_action_count,
        note: w.proof_card.window_clv_analog.note,
      },
      falsification: {
        verdict: w.proof_card.falsification.verdict,
        headline: w.proof_card.falsification.headline,
        delta_bps: w.proof_card.falsification.delta_bps,
        ci_low_bps: w.proof_card.falsification.ci_low_bps,
        ci_high_bps: w.proof_card.falsification.ci_high_bps,
      },
    },
    diagnostics: {
      avg_markout_bps_label: w.diagnostics.avg_markout_bps_label,
      avg_toxicity_loss_bps_label: w.diagnostics.avg_toxicity_loss_bps_label,
      real_executable_edge_bps_label: w.diagnostics.real_executable_edge_bps_label,
    },
  };
}

function emptyMatch(): MatchState {
  return { fixture: '', phase: 'NS', minute: null, goals: [0, 0], yellow: [0, 0], red: [0, 0], corners: [0, 0], status: 'scheduled' };
}

export function adaptCompetitionState(w: W.CompetitionStateResponse): CockpitState {
  const cfg = w.config as { fixture?: string; competition?: string; source_mode?: string; execution_mode?: string };
  return {
    competition_id: w.competition_id,
    run_id: w.run_id ?? '',
    header: {
      fixture: cfg.fixture ?? '',
      competition: cfg.competition ?? '',
      source_mode: toSourceMode(String(cfg.source_mode ?? 'replay')),
      execution_mode: (cfg.execution_mode as ExecutionMode) ?? 'paper',
      proof_mode: 'reproducible', // GAP: not in wire competition config
      events: w.latest_seq,
      valid_pct: 0, // GAP (PERCENT 0-100 convention; no header source in wire competition state)
      // Single source: the verifier the Proof Card shows is carried in the run's proof_card.
      // Fall back to the canonical const only when the competition has no sealed run yet.
      verifier_version: w.proof_card?.verifier_version ?? VERIFIER_VERSION,
    },
    // GAPs (WS-filled): trace/match/events/policy/kill_armed are NOT in GET /competitions/{id};
    // useArenaStream projects them from the live canonical stream.
    trace: [],
    match: emptyMatch(),
    // F-5: the leaderboard + receipts ARE on the competition-scoped response — bind them here so the
    // cockpit centerpiece populates from the real backend (backend-authoritative rank; sealed receipts),
    // and a SCORE_UPDATE refetch keeps them live. NEVER the global cross-run /leaderboard.
    leaderboard: adaptCompetitionLeaderboard(
      (w.leaderboard as unknown as CompetitionLeaderboardRowWire[]) ?? [],
      toSourceMode(String(cfg.source_mode ?? 'replay')),
      toAnchorStatus(w.anchor_status),
    ),
    events: [],
    receipts: adaptExecutionReceipts(w.execution),
    policy: [],
    kill_armed: false,
  };
}

export function adaptInspector(w: W.InspectorRecord): InspectorRecord {
  const rec = w.recompute as { recomputed_edge_bps?: number; clv_bps?: number | string; valid?: boolean; real_venue_quote?: boolean };
  const llm = w.untrusted_llm_metadata as { reason?: string; confidence?: number; claimed_edge_bps?: number; model?: string };
  // PENDING CLV (defect 2): the backend emits the non-numeric "pending" sentinel for a valid
  // WAIT/abstention — clv_bps is typed `int | str` (veridex/api/schemas.py:290; router.py:796). This
  // is DISTINCT from the F-5/R-globalclv null/unscored path (that renders "—"): pending is "too little
  // runway to score yet". Flag it so the screen shows an honest PENDING affordance and NEVER a
  // fabricated 0 — the pre-fix `Number('pending') || 0` silently coerced it to a scored-looking 0 bps.
  const clvPending = w.clv_bps === 'pending' || rec.clv_bps === 'pending';
  const clv = typeof w.clv_bps === 'number' ? w.clv_bps : Number(w.clv_bps) || 0;
  // DETERMINISTIC vs LLM (defect 6): the backend populates untrusted_llm_metadata ONLY from an LLM
  // action's {reason, confidence, claimed_edge_bps} params — it is the EMPTY dict {} for a
  // deterministic agent that emits none (router.py:803-805). An empty {} is truthy in JS, so key off
  // its CONTENTS: no keys ⇒ null (deterministic — no LLM in the provenance story), never a fabricated
  // zero-valued LLM record.
  const hasLlm = llm != null && Object.keys(llm).length > 0;
  return {
    run_id: w.run_id,
    agent_id: w.agent_id,
    action_seq: w.tick_seq,
    proof_mode: 'reproducible', // GAP: not in wire InspectorRecord
    is_live: false, // GAP
    market_state: w.market_state as unknown as InspectorRecord['market_state'],
    agent_action: w.agent_action as unknown as InspectorRecord['agent_action'],
    // The recompute echo is a TRUST SURFACE (a judge verifies the deterministic recompute here): a
    // non-numeric "pending" sentinel is preserved as null (honest "not scored yet"), NEVER the coerced
    // 0 — consistent with the D2 headline PENDING treatment and F-5/R-globalclv's null-preservation.
    recompute: { recomputed_edge_bps: rec.recomputed_edge_bps ?? 0, clv_bps: typeof rec.clv_bps === 'number' ? rec.clv_bps : null, valid: rec.valid ?? false },
    // GAP: the wire InspectorRecord carries no doctrine quantities (fair value,
    // executable edge, venue price, mispricing gap, stake) → null = honest "not in proof
    // artifact" (rendered as "—"), NOT a plausible 0. CLV (the real score) is carried through.
    // real_venue_quote is PROPAGATED from the wire (REQ-2D-701 gate 4), never hardcoded — it is
    // true ONLY when the backend earned it from a genuine venue quote. The live InspectorRecord
    // carries no venue quote today, so this reads false; the display gate still fails closed so an
    // edge number can NEVER render without a real quote (REQ-2D-501).
    clv_explanation: {
      fair_value_pct: null, closing_fair_value_pct: null, venue_decimal_price: null,
      mispricing_gap_bps: null, executable_edge_bps: null, real_venue_quote: rec.real_venue_quote === true,
      clv_bps: clv, clv_pending: clvPending, stake_fraction: null,
      plain: '',
    },
    untrusted_llm: hasLlm
      ? { model: llm.model ?? '', confidence: llm.confidence ?? 0, claimed_edge_bps: llm.claimed_edge_bps ?? 0, rationale: llm.reason ?? '' }
      : null,
  };
}

// GET /feed/health (WD-4) → view-model. Telemetry only (never scored). Carries the honesty
// signals verbatim — `ws_live`/`connected`/`stale`/`staleness_s` are the real feed state, never
// coerced to look healthy/live. `source_mode` rides the same honesty axis as every other reader.
export function adaptFeedHealth(w: W.FeedHealth): FeedHealthState {
  return {
    source_mode: toSourceMode(w.source_mode),
    ws_live: w.ws_live,
    connected: w.connected,
    txline_configured: w.txline_configured,
    events_per_min: w.events_per_min,
    ticks_seen: w.ticks_seen,
    staleness_s: w.staleness_s,
    stale: w.stale,
    fixture_id: w.fixture_id,
    anchor_status: toAnchorStatus(w.anchor_status),
    last_tick_ts: w.last_tick_ts,
  };
}

// MOCK MODE: demote any `live` source_mode → `replay` so fixtures never render under a LIVE
// badge (DEMO data is replay/recorded, never live — doctrine).
const demote = (s: SourceMode): SourceMode => (s === 'live' ? 'replay' : s);

// ---- readers / control ----
// Each reader short-circuits to the canonical wire fixture (via the SAME adapter) when mock is
// on — so the screen populates to its full state from contracts/fixtures, never the backend.
export async function getProofArtifact(runId: string): Promise<ProofArtifact> {
  if (isMockEnabled()) {
    // Overlay the DEMO root-forest (real names + demo hex) — mock only. Live maps the served forest
    // (currently absent ⇒ honest-empty). REPLAY source (demoted), never LIVE.
    const p = adaptProofArtifact(MOCK_FIXTURES.proofArtifact);
    return { ...p, source_mode: demote(p.source_mode), roots: PROOF_DEMO_ROOTS };
  }
  return adaptProofArtifact(await getJson<W.ProofArtifact>(PATHS.runProof(runId)));
}

export async function verifyProof(runId: string): Promise<VerifyResult> {
  if (isMockEnabled()) return adaptVerify(MOCK_FIXTURES.verify);
  return adaptVerify(await postJson<W.VerifyResult>(PATHS.verify(runId)));
}

// Proof Explainer (Phase B) — POST /runs/{id}/explain. READ-ONLY, non-scoring. Returns ONLY the
// {explanation, disclaimer, footer} envelope. Mock ⇒ a DEMO narration (same disclaimer/footer),
// never a live call. NOTE: validity/pass questions are short-circuited to the FIXED template in the
// UI BEFORE this reader is ever called — the LLM never answers "is this valid?".
export async function explainProof(
  runId: string, opts?: { question?: string; target_field?: string },
): Promise<ProofExplanation> {
  if (isMockEnabled()) return PROOF_EXPLAIN_DEMO;
  return postJson<ProofExplanation>(PATHS.explain(runId), opts ?? {});
}

export async function getLeaderboard(competitionId?: string): Promise<LeaderboardRow[]> {
  if (isMockEnabled()) {
    // leaderboard source_mode is SourceMode|'mixed' — demote only `live`, keep replay/mixed.
    return adaptLeaderboard(MOCK_FIXTURES.leaderboard).map((r) => ({ ...r, source_mode: r.source_mode === 'live' ? 'replay' : r.source_mode }));
  }
  return adaptLeaderboard(await getJson<W.LeaderboardResponse>(PATHS.leaderboard(competitionId)));
}

// ---- QuoteGuard behavior ablation (F-8 · maker_live_ab.v1) ----
//
// The guard OFF vs ON behavior ablation for a maker instance. It is a BEHAVIOR comparison (does the
// guard change the decision on the same recorded tape?), NEVER a rank / toxicity / edge / winner —
// the wire envelope carries no such field and this adapter never synthesizes one. 404 is a REAL,
// honest state ("no recorded ablation for this instance"), distinct from a transport error: the
// reader returns `null` on 404 and THROWS on any other non-ok so the screen renders the two states
// differently (unavailable vs error+retry), never a fabricated ablation (T-2 fixture prohibition).

/** Wire shape of one ablation arm (guard_off / guard_on) — the backend `_project_arm` dict. */
interface GuardAblationArmWire {
  guard_enabled?: boolean;
  terminal_reason?: string | null;
  observations_consumed?: number;
  decisions?: {
    index?: number;
    kind?: string;
    reason_codes?: string[];
    legs?: { kind?: string; role?: string; price?: number | null; post_only?: boolean }[];
  }[];
}

/** Wire shape of the `GET /maker/live-ab/{instance_id}` envelope (LiveGuardAblationResponse). */
interface GuardAblationResponseWire {
  schema_version?: string;
  lane?: string;
  panel?: string;
  is_ablation?: boolean;
  instance_id: string;
  mode: string;
  guard_off: GuardAblationArmWire;
  guard_on: GuardAblationArmWire;
  divergent_frame_indices?: number[];
  diverges?: boolean;
  labels?: Record<string, string>;
}

function adaptGuardAblationLeg(l: NonNullable<NonNullable<GuardAblationArmWire['decisions']>[number]['legs']>[number]): GuardAblationLeg {
  return {
    kind: String(l.kind ?? ''),
    role: String(l.role ?? ''),
    // price is honest: a real number when priced, null otherwise — NEVER coerced to 0 (a 0 odds
    // would be a fabricated quote). typeof-guard so a wire null/undefined stays null.
    price: typeof l.price === 'number' ? l.price : null,
    post_only: l.post_only === true,
  };
}

function adaptGuardAblationArm(a: GuardAblationArmWire): GuardAblationArm {
  return {
    guard_enabled: a.guard_enabled === true,
    terminal_reason: a.terminal_reason ?? null, // null = honest "no terminal reason", never invented
    observations_consumed: a.observations_consumed ?? 0,
    decisions: (a.decisions ?? []).map((d): GuardAblationDecision => ({
      index: d.index ?? 0,
      kind: String(d.kind ?? ''),
      reason_codes: d.reason_codes ?? [],
      legs: (d.legs ?? []).map(adaptGuardAblationLeg),
    })),
  };
}

export function adaptGuardAblation(w: GuardAblationResponseWire): GuardAblationView {
  return {
    schema_version: w.schema_version ?? 'maker_live_ab.v1',
    lane: w.lane ?? 'maker',
    panel: w.panel ?? 'guard_on_off_ablation',
    is_ablation: w.is_ablation !== false,
    instance_id: w.instance_id,
    mode: w.mode,
    guard_off: adaptGuardAblationArm(w.guard_off),
    guard_on: adaptGuardAblationArm(w.guard_on),
    divergent_frame_indices: w.divergent_frame_indices ?? [],
    diverges: w.diverges === true,
    labels: w.labels ?? {},
  };
}

// DEMO ablation for MOCK MODE only (recorded replay — never a live claim; the app-wide "DEMO DATA ·
// MOCK MODE" indicator makes this honest). Divergent example: the guard suppresses a toxic quote on
// three frames. Live (mock off) NEVER falls back to this — it 404s → null (unavailable) honestly.
const MOCK_GUARD_ABLATION: GuardAblationView = {
  schema_version: 'maker_live_ab.v1', lane: 'maker', panel: 'guard_on_off_ablation', is_ablation: true,
  instance_id: 'mm-inst-0f74a4', mode: 'replay',
  guard_off: {
    guard_enabled: false, terminal_reason: 'tape_exhausted', observations_consumed: 1024,
    decisions: [
      { index: 211, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [
        { kind: 'BID', role: 'maker', price: 2.34, post_only: true },
        { kind: 'ASK', role: 'maker', price: 2.51, post_only: true }] },
      { index: 212, kind: 'QUOTE', reason_codes: ['wide_ok', 'inv_room'], legs: [
        { kind: 'BID', role: 'maker', price: 2.34, post_only: true },
        { kind: 'ASK', role: 'maker', price: 2.51, post_only: true }] },
      { index: 213, kind: 'QUOTE', reason_codes: ['wide_ok'], legs: [
        { kind: 'BID', role: 'maker', price: 2.30, post_only: true }] },
      { index: 214, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [] },
      { index: 640, kind: 'QUOTE', reason_codes: ['wide_ok'], legs: [
        { kind: 'BID', role: 'maker', price: 1.98, post_only: true }] },
    ],
  },
  guard_on: {
    guard_enabled: true, terminal_reason: 'guard_halt', observations_consumed: 1024,
    decisions: [
      { index: 211, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [
        { kind: 'BID', role: 'maker', price: 2.34, post_only: true },
        { kind: 'ASK', role: 'maker', price: 2.51, post_only: true }] },
      { index: 212, kind: 'SUPPRESS', reason_codes: ['guard_toxicity_block'], legs: [] },
      { index: 213, kind: 'SUPPRESS', reason_codes: ['guard_cooldown'], legs: [] },
      { index: 214, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [] },
      { index: 640, kind: 'SUPPRESS', reason_codes: ['guard_toxicity_block'], legs: [] },
    ],
  },
  divergent_frame_indices: [212, 213, 640], diverges: true,
  labels: {
    panel_kind: 'behavior_ablation_guard_off_vs_on',
    comparison_basis: 'same strategy, same pinned tape — only the QuoteGuard arm differs',
    panel_disclaimer: 'this demonstrates the guard CHANGES behavior; it is NOT a rank / toxicity / performance ordering / winner and is never conflated with the sealed historical maker leaderboard',
    divergence_scope: 'divergence is what the two arms actually did on this tape — expected on the pinned adversarial trigger frame, may be empty on a quiescent stretch; never a promise the guard always diverges',
  },
};

// GET /maker/live-ab/{instanceId} → the guard behavior ablation, or `null` when the backend has no
// recorded ablation for the instance (404 — an honest "unavailable" state, NOT an error). Any other
// non-ok THROWS ApiError so the screen shows its error/retry state with no fabricated values. Mock ⇒
// the DEMO ablation (recorded replay). The route is public (read-only), so a plain accept-JSON GET.
export async function getMakerLiveAb(instanceId: string): Promise<GuardAblationView | null> {
  if (isMockEnabled()) return MOCK_GUARD_ABLATION;
  // Owner-scoped (auth-contract@1): the route is now bearer-authed + ownership-checked server-side, so
  // this MUST attach the Privy bearer (authedGet) — a plain fetch would 401. 404 is the honest "no
  // recorded ablation for this instance" (unknown / directional / non-maker); other non-ok throws.
  const path = PATHS.makerLiveAb(instanceId);
  const res = await authedGet(path);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return adaptGuardAblation((await res.json()) as GuardAblationResponseWire);
}

// GET /maker/arena-result → maker view-model (SEC-005: never routed through the CLV leaderboard).
// Mock ⇒ the canonical maker fixture (REPLAY, never LIVE), through the SAME adapter.
export async function getMakerArenaResult(): Promise<MakerArenaResultView> {
  if (isMockEnabled()) {
    const m = adaptMakerArenaResult(MOCK_FIXTURES.makerArenaResult);
    return { ...m, source_mode: demote(m.source_mode) };
  }
  return adaptMakerArenaResult(await getJson<W.MakerArenaResultResponseWire>(PATHS.makerArenaResult()));
}

export interface ReplayPackView {
  packId: string;
  contentHash: string;
  provenance: string;
  isGenuine: boolean;
  fixtures: number[];
  fixtureMetadata: W.ReplayPackFixtureMetaWire[];
}

// The deployed 1-pack/4-fixture Replay Library. Off-mock ⇒ the real /replay-packs fetch; mock ⇒ []
// (there is NO replay-pack demo fixture — honest-empty, never fabricated). Raw fixture ids are
// preserved; labels come SERVER-side (fixture_metadata), never a frontend-duplicated map (spec §5.2).
export async function getReplayPacks(): Promise<ReplayPackView[]> {
  if (isMockEnabled()) return [];
  const res = await getJson<W.ReplayPackListResponseWire>(PATHS.replayPacks());
  return res.packs.map((p) => ({
    packId: p.pack_id,
    contentHash: p.content_hash,
    provenance: p.provenance,
    isGenuine: p.is_genuine,
    fixtures: p.fixtures,
    fixtureMetadata: p.fixture_metadata ?? [],
  }));
}

// The SHARED replay-pack → FixtureSummary mapper (spec §5.2). Each pack's server-side fixture_metadata
// becomes a FixtureSummary carrying the COMPOSITE (pack_id, fixture_id) identity — two packs can share
// an external fixture_id, so pack_id must ride through. Labels are SERVER-sourced with an honest
// fallback when a team name is absent (never a fabricated name); kickoff_ts is epoch SECONDS on the
// wire → ISO string, absent ⇒ '' (honest, never faked). Consumed by /markets AND the Create-Competition
// fixture picker so their catalog view is byte-identical (single source of truth, no forked mapper).
export function replayPacksToFixtures(packs: ReplayPackView[]): FixtureSummary[] {
  return packs.flatMap((pack) =>
    pack.fixtureMetadata.map((m): FixtureSummary => ({
      fixture_id: m.fixture_id,
      pack_id: pack.packId,
      competition: pack.packId,
      participant1: m.home_team ?? `id ${m.fixture_id}`,
      participant2: m.away_team ?? '—',
      start_time: m.kickoff_ts != null ? new Date(m.kickoff_ts * 1000).toISOString() : '',
      in_running: false, // replay catalog — never in-running
    })),
  );
}

// ---- E2 replay-market projection (GET /replay-packs/{pack}/fixtures/{fixture}/markets) ----
//
// Maps the backend's LAST-KNOWN per-market projection to the screen's OddsUpdate view (buildFamilies
// renders it). MAJOR-5 EXACT unit conversion (verified against wire semantics):
//   • stable_price[outcome] = DECIMAL odds (e.g. 2.08) → prices[i] = round(x * 1000) (2.08 → 2080),
//     which decodePrice() renders back to 2.08.
//   • stable_prob_bps[outcome] = BPS (e.g. 5000 = 50%) → pct[i] = (bps / 100).toFixed(3) ("50.000").
//   • an outcome ABSENT from stable_prob_bps (suspended) → pct[i] = '' (the honest MISSING sentinel);
//     the render maps '' → '—', NEVER a fabricated "0.000". The retained stable_price still renders.

/** Wire shape of one projected market (backend ReplayMarketRow, veridex/api/schemas.py). */
interface ReplayMarketRowWire {
  market_key: string;
  in_running: boolean;
  suspended: boolean;
  ts: number;
  stable_prob_bps: Record<string, number>;
  stable_price: Record<string, number>;
}

/** Wire shape of the GET .../markets envelope (backend ReplayMarketsResponse). */
interface ReplayMarketsResponseWire {
  fixture_id: number;
  label: string;
  markets: ReplayMarketRowWire[];
}

// One wire market → one OddsUpdate. price_names is the DETERMINISTIC outcome order = the keys of the
// wire stable_price map (every priced outcome, stable insertion order). market_family = the FIRST
// segment of market_key ({SuperOddsType}|{MarketPeriod}|{MarketParameters}); market_parameters = the
// THIRD segment or null. Only the 3 known MarketFamilyKeys render (buildFamilies filters the rest).
function replayMarketToOddsUpdate(fixtureId: number, m: ReplayMarketRowWire): OddsUpdate {
  const outcomes = Object.keys(m.stable_price); // deterministic: the priced outcomes, stable order
  // market_key is {SuperOddsType}|{MarketPeriod}|{MarketParameters}. Take the params from index 2 ONWARD
  // (join back any pipes) so a MarketParameters segment that itself contains '|' is preserved, not truncated.
  const parts = m.market_key.split('|');
  const superOddsType = parts[0];
  const marketParameters = parts.slice(2).join('|') || null;
  return {
    fixture_id: fixtureId,
    message_id: m.market_key, // stable, deterministic id (the projection is one row per market_key)
    ts: m.ts,
    in_running: m.in_running,
    // The RAW SuperOddsType is preserved verbatim; buildFamilies renders ONLY the known families and
    // drops any other (the cast is safe — the value is only compared against MARKET_FAMILY_KEYS + text).
    market_family: superOddsType as MarketFamilyKey,
    market_parameters: marketParameters,
    price_names: outcomes, // deterministic outcome order = the priced-outcome keys (stable)
    prices: outcomes.map((o) => Math.round(m.stable_price[o] * 1000)), // decimal → int ×1000
    // implied %: bps → 3dp string; ABSENT (suspended) ⇒ '' sentinel → render maps to '—', never a fake 0.
    pct: outcomes.map((o) =>
      Object.prototype.hasOwnProperty.call(m.stable_prob_bps, o) ? (m.stable_prob_bps[o] / 100).toFixed(3) : '',
    ),
  };
}

// GET the E2 projection → OddsUpdate[] (buildFamilies-ready). Off-mock only — under mock the page keeps
// its existing demo odds path (this reader is never called there). Honest-empty [] on any transport
// error (never a fabricated market). Suspended markets carry retained prices with '' implied sentinels.
export async function getReplayMarkets(packId: string, fixtureId: number): Promise<OddsUpdate[]> {
  try {
    const res = await getJson<ReplayMarketsResponseWire>(PATHS.replayMarkets(packId, fixtureId));
    return res.markets.map((m) => replayMarketToOddsUpdate(fixtureId, m));
  } catch {
    return []; // honest-empty on error — the screen shows absence, never a fabricated market
  }
}

// ---- PUBLIC deployed-agent roster (GET /agents/roster) ----
//
// The unauthenticated public roster of ALL deployed instances across ALL owners (mirrors
// /replay-packs — read-only, no bearer). Distinct from the OWNER-scoped getInstances()
// (/agents/instances). The backend projects public-safe deployment identity only; the performance
// columns are ALWAYS null (no scoring aggregation exists yet) — surfaced as null so the table
// renders "—", NEVER fabricated.

/** Wire shape of one public roster row (frozen backend AgentRosterEntry, veridex/api/schemas.py:470).
 * TRUST SURFACE: it carries the SAFE public identity only (public_agent_id / display_name /
 * owner_public_label / origin) — NEVER a raw operator_id / owner_ref. Performance columns are None and
 * proof_state is "unscored" until the agent has scored PUBLIC_AGENTS board rows. */
export interface AgentRosterEntryWire {
  public_agent_id: string;
  display_name: string;
  owner_public_label: string; // SAFE public owner rendering (brand / shortened wallet / em-dash)
  origin: string; // the real Origin value ("official" | "byoa" | "unknown" | ...)
  proof_state: string; // "unscored" until scored, then the real proof mode
  agent_id: string; // the deployed instance id (informational; the directory keys on public_agent_id)
  type: string; // template_id (strategy archetype)
  source_mode: string; // replay | live
  execution_mode: string;
  status: string; // DeployStatus value, lowercased (always "sealed")
  config_hash_present: boolean; // REAL proof indicator (config_hash pinned) — not a score
  avg_clv_bps: number | null; // null until scored, then the REAL pooled value — never fabricated
  runs: number | null;
  valid_pct: number | null;
}

export interface AgentRosterResponseWire {
  agents: AgentRosterEntryWire[];
}

// template_id is the deployed archetype label for the archetype path (StudioScreen sends
// template_id=archetype); MM/drift templates carry their own id. The RAW template_id is preserved
// VERBATIM as the archetype (rendered as-is in the ARCHETYPE column — the real deployment identity),
// never remapped to a fabricated archetype. The cast is safe: the value is only rendered as text.
function toArchetype(templateId: string): Archetype {
  return templateId as Archetype;
}

// The wire proof_state → the roster-local ProofState. An EARNED single-mode claim (verified / partial /
// reproducible) is preserved verbatim; 'unscored' (the honest pre-score state) and 'mixed' (the backend's
// honest cross-run aggregate for runs with different proof modes — veridex/leaderboard.py) are preserved
// as their own honest states. ANY OTHER string fails CLOSED to 'unknown' — it is NEVER coerced up to an
// unearned 'reproducible' proof claim (Gate-3 M3). This does NOT delegate to toProofMode (which would
// overclaim unknown→reproducible) and does NOT touch the shared ProofMode — 'unscored'/'mixed'/'unknown'
// live ONLY on ProofState (roster-local).
function toProofState(s: string): ProofState {
  if (s === 'verified' || s === 'partial' || s === 'reproducible') return s;
  if (s === 'mixed') return 'mixed';
  if (s === 'unscored') return 'unscored';
  return 'unknown'; // any unexpected string ⇒ honest 'unknown', NEVER a fabricated 'reproducible'
}

// GET /agents/roster → PublicAgentRow[] for the AgentsScreen directional table. Mock ⇒ [] (the page
// supplies the labeled DEMO fixture, mapped through the SHARED agentSummaryToPublicRow adapter, under
// the mock gate; this reader is the OFF-mock real-fetch path). Honest identity is mapped verbatim
// (public_agent_id / display_name / owner_public_label / origin); proof_state carries the REAL wire
// value ("unscored" until scored); performance columns map to their honest null/number ("—", never
// fabricated). Honest-empty [] on any fetch error — NEVER the AGENTS fixture off-mock (T-2).
export async function getAgentsRoster(): Promise<PublicAgentRow[]> {
  if (isMockEnabled()) return [];
  try {
    const res = await getJson<AgentRosterResponseWire>(PATHS.agentsRoster());
    return res.agents.map((e) => ({
      public_agent_id: e.public_agent_id,
      display_name: e.display_name,
      owner_public_label: e.owner_public_label,
      origin: e.origin,
      proof_state: toProofState(e.proof_state),
      archetype: toArchetype(e.type),
      mode: null, // the roster carries no strategy mode (llm|numeric|rule) — honest "—", never fabricated
      avg_clv_bps: e.avg_clv_bps, // null until scored — honest "—", never fabricated
      runs: e.runs,
      valid_pct: e.valid_pct,
    }));
  } catch {
    return []; // honest-empty on error — NEVER the AGENTS fixture off-mock (T-2 fixture prohibition)
  }
}

// ---- B3 directional leaderboard completion layer (GET /leaderboard/directional) ----
//
// The cross-run directional board ENRICHED with honest public identity. The wire row is a
// LeaderboardRow PLUS { display_name, public_agent_id } (the DirectionalRow enrichment). The reader
// runs each row through the SAME base leaderboard mapping (adaptLeaderboard) and then overrides the
// opaque-id identity with the REAL public identity: agent_id = public_agent_id (the link/key) and
// agent_name = display_name (the REAL display name, NEVER the opaque-id fallback). source_mode rides
// the same aggregate mapping (all-replay → replay), so the board survives the REPLAY filter (M8).

/** Wire shape of one directional row (backend LeaderboardRow + the display_name/public_agent_id join). */
interface DirectionalRowWire extends W.LeaderboardRow {
  display_name: string;
  public_agent_id: string;
}

/** Wire shape of the GET /leaderboard/directional envelope. */
interface DirectionalLeaderboardResponseWire {
  board_kind: string;
  rows: DirectionalRowWire[];
}

export function adaptDirectionalLeaderboard(w: DirectionalLeaderboardResponseWire): DirectionalRow[] {
  return adaptLeaderboard({ rows: w.rows }).map((base, i) => ({
    ...base,
    agent_id: w.rows[i].public_agent_id, // key/link by the public id
    agent_name: w.rows[i].display_name,  // the REAL display name, not the opaque-id fallback
    public_agent_id: w.rows[i].public_agent_id,
    display_name: w.rows[i].display_name,
    // HONEST board proof state (Gate-3 M3): map the wire proof_mode via toProofState — the backend's
    // cross-run aggregate "mixed" stays 'mixed' (never the unearned 'reproducible' that base.proof_mode
    // coerces it to), and any unrecognized value fails CLOSED to 'unknown'.
    proof_state: toProofState(w.rows[i].proof_mode),
  }));
}

// GET /leaderboard/directional?board_kind=public_agents → DirectionalRow[] (honest display names +
// replay provenance). board_kind is the LOWERCASE closed wire enum the backend accepts (an uppercase
// value 422s → the board would render empty off-mock). Mock ⇒ [] (the /leaderboard page keeps its
// mock-ON getLeaderboard() fixture path). Off-mock the LeaderboardPage calls this and honest-empties on
// error (never a wire fixture).
export async function getDirectionalLeaderboard(boardKind: BoardKindWire = 'public_agents'): Promise<DirectionalRow[]> {
  if (isMockEnabled()) return [];
  const res = await getJson<DirectionalLeaderboardResponseWire>(PATHS.directionalLeaderboard(boardKind));
  return adaptDirectionalLeaderboard(res);
}

// ---- Quick honest enrichment: getAgentProfile (leaner REAL profile, NO new backend endpoint) ----
//
// Mock ⇒ the AGENT_PROFILES fixture UNCHANGED (preserves today's mock behavior EXACTLY). Off-mock ⇒ a
// REAL (leaner) profile assembled from data we ALREADY serve: the directional public_agents board
// (per-agent aggregates) + the public roster (identity). There is NO agent-profile endpoint, so fields
// those two readers don't carry degrade HONESTLY (config_hash "—", empty competitions with
// breakdown_available:false, empty anchors) — NEVER fabricated. Not-found / any transport error ⇒ null
// (honest-unavailable, never a fixture off-mock — T-2 fixture prohibition).
export async function getAgentProfile(publicAgentId: string): Promise<AgentProfileRecord | null> {
  if (isMockEnabled()) return AGENT_PROFILES[publicAgentId] ?? null;
  try {
    const [board, roster] = await Promise.all([
      getDirectionalLeaderboard('public_agents'),
      getAgentsRoster(),
    ]);
    const row = board.find((r) => r.public_agent_id === publicAgentId);
    if (!row) return null; // honest not-found — no such agent on the board
    const identity = roster.find((r) => r.public_agent_id === publicAgentId);
    // `source` CLASSIFIES real AUTHORSHIP from the roster's real `origin` — the only honest signal for
    // first-party (STUDIO) vs third-party (BYOA). proof_mode is ORTHOGONAL to authorship (and
    // toProofMode coerces mixed/unknown/unscored/'' → 'reproducible'), so it must NOT feed this — using
    // it would falsely stamp STUDIO on BYOA agents. Roster-absent ⇒ BYOA (the least-claim, never a
    // fabricated first-party claim).
    const source: 'STUDIO' | 'BYOA' = identity?.origin === 'official' ? 'STUDIO' : 'BYOA';
    return {
      agent_id: row.public_agent_id,
      agent_name: row.display_name,
      // archetype comes from the roster identity. The shared `Archetype` union has no honest "unknown"
      // member and widening it is out of scope, so for the roster-absent edge (e.g. the roster fetch
      // failed → [] while the board succeeded) we store the em-dash absent-marker rather than SEEDING a
      // specific strategy like 'baseline' — an unearned claim. The value is display-only text here
      // (rendered verbatim in the header), and the cast follows this file's existing free-string
      // `x as Archetype` convention (see toArchetype). Honest "—", never a fabricated archetype.
      archetype: identity?.archetype ?? ('—' as Archetype),
      mode: identity?.mode ?? null,
      avg_clv_bps: row.avg_clv_bps,
      runs: row.runs,
      proof_mode: row.proof_mode,
      source_mode: row.source_mode,
      valid_pct: row.valid_pct,
      source,
      valid_count: row.valid_count,
      // config_hash / policy_hash are not exposed by any endpoint — the standard absent marker.
      config_hash: '—',
      policy_hash: '—',
      // Honest note: the strategy configuration is not surfaced here (NOT a fabricated strategy blurb).
      strategy_caption: 'Strategy configuration is not exposed on the public agent profile.',
      // No per-competition breakdown from these endpoints (honest-empty). breakdown_available:false makes
      // the screen render an honest "not exposed" note instead of implying zero completed competitions.
      completed_competitions: [],
      // Honest-empty: neither the directional board nor the roster exposes any per-anchor
      // tx_signature/slot, so there is no honest anchor entry to show (absent, independent of the
      // board's aggregate anchor_status which this reader does not read).
      anchors: [],
      deployment_provenance: identity
        ? `${identity.owner_public_label} · origin ${identity.origin}`
        : 'Deployment provenance is not exposed on the public agent profile.',
      total_clv_bps: row.total_clv_bps,
      eligibility_badge: row.eligibility_badge,
      breakdown_available: false,
    };
  } catch {
    return null; // honest-unavailable on any transport error — NEVER a fabricated/fixture profile off-mock
  }
}

export interface CompetitionRecordView {
  competitionId: string;
  status: string;
  title: string;
  // Surfaced ONLY from the server record's config. Absent → null (the caller renders "—"), NEVER a
  // fabricated 'replay'/'paper' default — that would violate the absent-value honesty rule (spec §7).
  sourceMode: string | null;
  executionMode: string | null;
  rosterSize: number | null;
  runId: string | null;
}

// The real backend competition records (GET /competitions). Surfaces ONLY server-provided fields;
// the title is derived from config.market_scope, else the raw competition_id (spec §6.1). Never
// reproduces aspirational mock values (prize/TVL/live counts/anchor) and never fabricates an absent
// source/exec/roster. Callers gate mock mode themselves.
export async function getCompetitions(): Promise<CompetitionRecordView[]> {
  const rows = await getJson<W.CompetitionSummaryWire[]>(PATHS.competitions());
  return rows.map((r) => {
    const cfg = (r.config ?? {}) as Record<string, unknown>;
    const scope = typeof cfg.market_scope === 'string' && cfg.market_scope.trim() ? cfg.market_scope : r.competition_id;
    return {
      competitionId: r.competition_id,
      status: r.status,
      title: scope,
      sourceMode: typeof cfg.source_mode === 'string' ? cfg.source_mode : null,
      executionMode: typeof cfg.execution_mode === 'string' ? cfg.execution_mode : null,
      rosterSize: typeof cfg.roster_size === 'number' ? cfg.roster_size : null,
      runId: r.run_id,
    };
  });
}

export async function getCockpitState(competitionId: string): Promise<CockpitState> {
  if (isMockEnabled()) {
    // Fixture-seeded REPLAY projection: honest header (source demoted) + the populated demo body.
    // Live (mock off) stays honest-empty until the WS fills it — no fabricated projection.
    const c = adaptCompetitionState(MOCK_FIXTURES.competition);
    return { ...c, header: { ...c.header, source_mode: demote(c.header.source_mode) }, ...COCKPIT_DEMO };
  }
  return adaptCompetitionState(await getJson<W.CompetitionStateResponse>(PATHS.competitionState(competitionId)));
}

export async function getInspectorRecord(runId: string, seq: number | string): Promise<InspectorRecord> {
  if (isMockEnabled()) {
    // Overlay the DEMO doctrine quantities (Fair Value / Executable Edge / stake) — mock only. Live
    // has none of these on the wire, so adaptInspector leaves them null ("—"). clv_bps travels through.
    const rec = adaptInspector(MOCK_FIXTURES.inspector);
    return { ...rec, clv_explanation: { ...rec.clv_explanation, ...INSPECTOR_DEMO_QUANTITIES } };
  }
  return adaptInspector(await getJson<W.InspectorRecord>(PATHS.inspector(runId, seq)));
}

export async function getFeedHealth(): Promise<FeedHealthState> {
  if (isMockEnabled()) {
    const h = adaptFeedHealth(MOCK_FIXTURES.feedHealth);
    return { ...h, source_mode: demote(h.source_mode) }; // a synthetic LIVE feed never renders LIVE
  }
  return adaptFeedHealth(await getJson<W.FeedHealth>(PATHS.feedHealth()));
}

// ---- Studio deploy (T21 — POST /agents/deploy) ----

/** Non-secret config the Studio deploy button submits (mirrors the backend DeployConfig). */
export interface DeployAgentPayload {
  template_id: string;
  agent_id: string;
  strategy: string;
  source_mode: 'replay' | 'live';
  execution_mode: ExecutionMode;
  market_allowlist: string[];
  venue_allowlist: string[];
  min_edge_bps: number;
  max_stake: number;
  window_id: string;
  fixture_id: number;
  end_rule: 'pre_match' | 'fixed_duration' | 'manual_stop';
  // fu-ii5: the maker/quote-guard subset, present ONLY for the `quoteguard-mm` MM family (the
  // backend dispatches on `strategy == "quoteguard-mm"`). Mirrors MakerDeployConfig (extra="forbid"),
  // so no unknown fields. `tape_ref` is a bounded catalog KEY (never a path/fixture).
  mm?: {
    tape_ref: string;
    guard_enabled?: boolean;
    tif?: 'GTC' | 'GTD';
    max_orders_per_run?: number;
    max_orders_per_session?: number;
    max_orders_per_day?: number;
    max_session_loss?: number;
    max_daily_loss?: number;
  };
}

/** The pinned-instance handle the deploy endpoint returns (run_id known BEFORE the seal). */
export interface DeployAgentResult {
  instance_id: string;
  config_hash: string;
  policy_hash: string;
  run_id: string;
}

/** One named preflight verdict as returned in the 422 detail. */
export interface DeployPreflightVerdict {
  name: string;
  ok: boolean | null;
  detail: string;
}

/** Thrown when the deploy preflight fails closed (HTTP 422) — carries the NAMED failing checks. */
export class DeployPreflightError extends Error {
  constructor(
    public failedChecks: string[],
    public checks: DeployPreflightVerdict[],
  ) {
    super(`deploy preflight failed: ${failedChecks.join(', ')}`);
    this.name = 'DeployPreflightError';
  }
}

/**
 * Deploy an agent instance: POST the config to /agents/deploy.
 *
 * On a fail-closed preflight (422) the backend returns ``{detail: {failed_checks, checks}}``; this
 * surfaces it as a {@link DeployPreflightError} so the UI can name the failing check instead of a
 * bare status. The 200 body is the pinned instance + the launched ``run_id`` (returned WITHOUT
 * awaiting the seal).
 *
 * auth-contract@1: owner-scoped — carries the bearer from lib/auth.ts when the seam has a token
 * (never fabricates one when it doesn't), and retries once on a 401 (see authedFetch). The
 * fail-closed "no token → never fires" guarantee is enforced at the UI layer by wrapping the
 * calling affordance in {@link AuthGate}, not by this function refusing to fetch.
 */
export async function deployAgent(
  payload: DeployAgentPayload,
  idempotencyKey?: string,
): Promise<DeployAgentResult> {
  // The Idempotency-Key HTTP header (I-3, deploy.py) makes the deploy idempotent: same
  // (operator_id, key) ⇒ the SAME instance. A client that reuses ONE stable key across a
  // retry/timeout reconciles to a single instance instead of minting duplicates. Absent ⇒ the
  // backend mints a fresh per-request key (client idempotency lost), so the caller owns the key.
  const res = await authedFetch(
    '/agents/deploy',
    payload,
    idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
  );
  if (res.status === 422) {
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    // The preflight 422 detail is a {failed_checks, checks} OBJECT. But a bare FastAPI request-
    // validation 422 returns `detail` as an ARRAY, and a malformed body may omit it — in those cases
    // there are no named checks. Never surface an EMPTY failed-checks list: the UI suppresses both the
    // success badge (gated `!preflightFailure`) AND the alert (gated `.length > 0`), so an empty list
    // renders NOTHING (silence). Fall back to a named `preflight_failed` so a 422 is always visible.
    const detail = (body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail))
      ? (body.detail as { failed_checks?: string[]; checks?: DeployPreflightVerdict[] })
      : {};
    const failed = detail.failed_checks && detail.failed_checks.length > 0
      ? detail.failed_checks
      : ['preflight_failed'];
    throw new DeployPreflightError(failed, detail.checks ?? []);
  }
  if (!res.ok) throw new ApiError(res.status, `POST /agents/deploy failed: ${res.status}`);
  return (await res.json()) as DeployAgentResult;
}

// ---- Owner-scoped deployed instances (I-2 · F-3) ----
//
// The DURABLE deployed-instance record — the OWNER-scoped deployment identity, distinct from the
// PUBLIC /agents strategy profile. Mirrors the frozen backend `AgentInstance`
// (veridex/deploy/instance.py): `run_id` is the AUTHORITATIVE Veridex evidence identity;
// `runtime_handle.session_id` is a REPLACEABLE AgentOS handle (re-minted on restart under the same
// run_id) — never the result/ownership authority. `operator_id` is the SERVER-DERIVED owner.

/** The runtime infra pointer — replaceable; NEVER the ownership/result authority (run_id is). */
export interface InstanceRuntimeHandle {
  runtime_kind: string;
  runtime_agent_id: string;
  session_id: string | null;
  run_id: string;
}

/** Wire shape of a deployed instance as served by GET /agents/instances[/{id}] (frozen contract). */
export interface AgentInstanceWire {
  instance_id: string;
  template_id: string;
  agent_id: string;
  config_hash: string;
  policy_hash: string;
  source_mode: string;
  execution_mode: string;
  market_allowlist: string[];
  venue_allowlist: string[];
  run_id: string;
  status: string; // DeployStatus value: pending | running | sealed | failed
  last_failure_reason: string | null;
  operator_id: string | null;
  runtime_handle: InstanceRuntimeHandle | null;
  created_at: string;
  updated_at: string; // durable last-write timestamp (present on the contract; not surfaced today)
  // Additive server-derived labels (owner-scoped GET /agents/instances/{id} only). Optional so the
  // list route + any older payload still validate. CURATED convenience labels (see backend
  // veridex/api/fixture_labels.py) — they AUGMENT the raw ids, never replace them.
  fixture_id?: number | null;
  fixture_label?: string | null;
  market_label?: string | null;
  // Two DISTINCT hash identities (never conflated): the R-4 replay PACK selection hash vs the
  // quoteguard-mm MakerReplayTape's OWN hash the maker run verifies. maker_tape_* are present only
  // for an MM instance.
  replay_pack_content_hash?: string | null;
  replay_pack_id?: string | null;
  maker_tape_ref?: string | null;
  maker_tape_content_hash?: string | null;
}

/** View-model of a deployed instance (owner-scoped identity the instance page + dashboard render). */
export interface DeployedInstance {
  instance_id: string;
  template_id: string;
  agent_id: string;
  run_id: string;
  status: string; // preserved VERBATIM — never coerced to a rosier lifecycle state (honesty)
  source_mode: SourceMode;
  execution_mode: string;
  config_hash: string;
  policy_hash: string;
  operator_id: string | null;
  runtime_handle: InstanceRuntimeHandle | null;
  last_failure_reason: string | null;
  market_allowlist: string[];
  venue_allowlist: string[];
  created_at: string;
  // CURATED, server-derived labels that AUGMENT the raw ids (present on the owner-scoped detail
  // response; absent on the list route + demo rows). Optional + null-honest; never a "verified" claim.
  fixture_id?: number | null;
  fixture_label?: string | null;
  market_label?: string | null;
  // DISTINCT hashes: replay_pack_content_hash is the R-4 pack selection hash; maker_tape_content_hash
  // is the quoteguard-mm MakerReplayTape's own hash (a different identity). Never present one as the other.
  replay_pack_content_hash?: string | null;
  replay_pack_id?: string | null;
  maker_tape_ref?: string | null;
  maker_tape_content_hash?: string | null;
}

export function adaptAgentInstance(w: AgentInstanceWire): DeployedInstance {
  const rh = w.runtime_handle;
  return {
    instance_id: w.instance_id,
    template_id: w.template_id,
    agent_id: w.agent_id,
    run_id: w.run_id, // authoritative evidence identity — carried through 1:1
    status: w.status, // verbatim (SEC honesty: a failed/pending instance never reads as sealed)
    source_mode: toSourceMode(w.source_mode),
    execution_mode: w.execution_mode,
    config_hash: w.config_hash,
    policy_hash: w.policy_hash,
    operator_id: w.operator_id ?? null,
    runtime_handle: rh
      ? {
          runtime_kind: String(rh.runtime_kind ?? ''),
          runtime_agent_id: String(rh.runtime_agent_id ?? ''),
          session_id: rh.session_id ?? null, // replaceable handle, null-honest
          run_id: String(rh.run_id ?? ''),
        }
      : null,
    last_failure_reason: w.last_failure_reason ?? null,
    market_allowlist: w.market_allowlist ?? [],
    venue_allowlist: w.venue_allowlist ?? [],
    created_at: w.created_at,
    // CURATED labels carried through null-honest — absent on the list route, so default to null.
    fixture_id: w.fixture_id ?? null,
    fixture_label: w.fixture_label ?? null,
    market_label: w.market_label ?? null,
    replay_pack_content_hash: w.replay_pack_content_hash ?? null,
    replay_pack_id: w.replay_pack_id ?? null,
    maker_tape_ref: w.maker_tape_ref ?? null,
    maker_tape_content_hash: w.maker_tape_content_hash ?? null,
  };
}

// DEMO instances for MOCK MODE only (source REPLAY — never rendered under a LIVE badge; the app-wide
// "DEMO DATA · MOCK MODE" indicator makes this honest). Live (mock off) NEVER falls back to these.
const MOCK_INSTANCES: DeployedInstance[] = [
  {
    instance_id: 'inst_demo_value_clv', template_id: 'value_clv', agent_id: 'studio-value_clv',
    run_id: 'run_demo_esp_ned', status: 'sealed', source_mode: 'replay', execution_mode: 'paper',
    config_hash: 'c'.repeat(64), policy_hash: 'p'.repeat(64), operator_id: 'did:privy:demo-operator',
    runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_demo_1', session_id: 'sess_demo_1', run_id: 'run_demo_esp_ned' },
    last_failure_reason: null, market_allowlist: ['moneyline'], venue_allowlist: ['polymarket'],
    created_at: '2026-07-15T12:00:00Z',
  },
  {
    instance_id: 'inst_demo_momentum', template_id: 'momentum', agent_id: 'studio-momentum',
    run_id: 'run_demo_fra_bra', status: 'running', source_mode: 'replay', execution_mode: 'paper',
    config_hash: 'd'.repeat(64), policy_hash: 'q'.repeat(64), operator_id: 'did:privy:demo-operator',
    runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_demo_2', session_id: 'sess_demo_2', run_id: 'run_demo_fra_bra' },
    last_failure_reason: null, market_allowlist: ['moneyline'], venue_allowlist: ['polymarket'],
    created_at: '2026-07-16T09:30:00Z',
  },
];

// GET /agents/instances — the caller's OWN deployed instances (owner-scoped, bearer-authed). Mock
// ⇒ the DEMO set (replay). Live ⇒ authedGet; a non-ok (401/403/5xx) throws so the caller can render
// an honest empty/error state — it NEVER silently falls back to a fixture (T-2 fixture prohibition).
export async function getInstances(): Promise<DeployedInstance[]> {
  if (isMockEnabled()) return MOCK_INSTANCES.map((i) => ({ ...i, source_mode: demote(i.source_mode) }));
  const res = await authedGet(PATHS.agentInstances());
  if (!res.ok) throw new ApiError(res.status, `GET ${PATHS.agentInstances()} failed: ${res.status}`);
  return ((await res.json()) as AgentInstanceWire[]).map(adaptAgentInstance);
}

// ---- Owner-scoped runtime-events (I-4 · F-6) ----
//
// One SERVED runtime event: the frozen wire RuntimeEvent (OPS-channel telemetry, SEC-003 — never
// sealed/scored) PLUS its durable BIGSERIAL `id`, the exclusive read-cursor the poller advances.
// The `agent_id` is already in the response body (wire RuntimeEvent), never re-derived here.
export interface RuntimeEventRecord extends W.RuntimeEvent {
  id: number;
}

// GET /agents/instances/{id}/runtime-events — the caller's OWN durable OPS telemetry for a deployed
// instance (owner-scoped, bearer-authed; same seam as getInstances). `since` is the EXCLUSIVE durable
// cursor (advance to max(id) across polls ⇒ no duplicates). A non-ok (401/403/404/5xx) THROWS so the
// drawer can render an honest empty/error state — it NEVER silently falls back to a fixture (T-2).
// No mock branch: runtime events have no frozen wire fixture; the drawer's demo path is gated at the
// hook (isMockEnabled) so the LIVE reader stays a dumb, honest transport that only ever hits the API.
export async function getRuntimeEvents(
  instanceId: string,
  since = 0,
  limit?: number,
): Promise<RuntimeEventRecord[]> {
  const path = PATHS.instanceRuntimeEvents(instanceId, since, limit);
  const res = await authedGet(path);
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  const body = (await res.json()) as { events: RuntimeEventRecord[] };
  return body.events;
}

// GET /agents/instances/{id} — ONE instance the caller owns. A 403 (owned by another) / 404 (absent
// or unowned legacy row) throws ApiError so the page renders an honest unauthorized/not-found state,
// never a fabricated instance.
export async function getInstance(instanceId: string): Promise<DeployedInstance> {
  if (isMockEnabled()) {
    const found = MOCK_INSTANCES.find((i) => i.instance_id === instanceId);
    // Honest 404 for an unknown id even in mock — never fabricate a first-demo success on the
    // not-found surface (InstanceScreen renders the honest not-found state).
    if (!found) throw new ApiError(404, `GET ${PATHS.agentInstance(instanceId)} failed: 404 (mock: unknown instance)`);
    return { ...found, source_mode: demote(found.source_mode) };
  }
  const res = await authedGet(PATHS.agentInstance(instanceId));
  if (!res.ok) throw new ApiError(res.status, `GET ${PATHS.agentInstance(instanceId)} failed: ${res.status}`);
  return adaptAgentInstance((await res.json()) as AgentInstanceWire);
}

// ---- Owner-scoped instance lifecycle (II-6 · F-7) ----
//
// The control-plane WRITE surface for the Agent Ops drawer: read a deployed instance's authoritative
// run/lease status, engage the owner-gated exactly-once kill, and (for "Disable execution") engage a
// competition's kill-switch. All owner-scoped through the SAME bearer + 401-retry seam as deployAgent
// / getInstances (never fabricates a bearer; never silently eats a 401). No mock branch: these are
// real control-plane mutations — the drawer's demo path is gated at the hook (isMockEnabled) so a
// kill is NEVER faked, and these readers stay dumb, honest transports that only ever hit the API.
// There is deliberately NO pauseInstance: the runtime has no pause/resume endpoint (shutdown-cancel
// only — deploy.py CON-2D-701), so the drawer keeps Pause/Resume honestly disabled instead of faking.

/** Owner-scoped run/lease status for a deployed instance (mirrors backend InstanceStatusResponse). */
export interface InstanceStatus {
  instance_id: string;
  run_id: string;
  // HEADLINE run/lease state: running | cancelled | sealed | failed | pending. Reflects an engaged
  // owner kill as `cancelled` (durable across the run settling) — carried through VERBATIM.
  run_state: string;
  killed: boolean; // whether an owner kill engaged the exactly-once cancel for this run
  status: string; // the DURABLE DeployStatus record value (pending | running | sealed | failed)
  lease_status: string | null;
}

/** Result of an owner kill (mirrors backend KillResponse). */
export interface KillResult {
  instance_id: string;
  run_id: string;
  phase: string; // RunPhase after this call: active | cancelling | completed | failed | cancelled
  engaged: boolean; // true ONLY for the single caller that engaged the exactly-once cancel
}

/** Result of a competition kill-switch engage (mirrors backend KillSwitchResponse). */
export interface KillSwitchResult {
  competition_id: string;
  kill_switch: boolean; // always true post-engage (engage-only, SAF-004)
  status: string; // "kill_switch_on" | "kill_switch_off"
}

// GET /agents/instances/{id}/status — the caller's OWN run/lease status. A non-ok (401/403/404/5xx)
// THROWS ApiError so the drawer renders an honest unauthorized/not-found/error state, never a
// fabricated status. The drawer refetches this after a kill to reflect the resulting terminal state.
export async function getInstanceStatus(instanceId: string): Promise<InstanceStatus> {
  const path = PATHS.instanceStatus(instanceId);
  const res = await authedGet(path);
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return (await res.json()) as InstanceStatus;
}

// POST /agents/instances/{id}/kill — engage the owner-gated exactly-once shutdown-cancel (no body).
// A non-ok THROWS: 403 owned-by-another, 404 absent/unowned, 409 no active run / not live — so a
// failed kill surfaces VISIBLY and is NEVER shown as a success. The 200 body names the resulting
// RunPhase + whether THIS caller engaged the cancel (a repeat kill returns engaged=false).
export async function killInstance(instanceId: string): Promise<KillResult> {
  const path = PATHS.instanceKill(instanceId);
  const res = await authedFetch(path, undefined); // POST, no body (kill takes no request payload)
  if (!res.ok) throw new ApiError(res.status, `POST ${path} failed: ${res.status}`);
  return (await res.json()) as KillResult;
}

// POST /competitions/{id}/kill-switch — ENGAGE (never toggle) the competition's kill-switch (no
// body). Engage-only + idempotent (SAF-004): the first engage stops trading; a retry keeps it
// engaged and re-opens nothing. A non-ok THROWS (401/403/404) so a failed engage surfaces visibly.
export async function armCompetitionKillSwitch(competitionId: string): Promise<KillSwitchResult> {
  const path = PATHS.competitionKillSwitch(competitionId);
  const res = await authedFetch(path, undefined); // POST, no body
  if (!res.ok) throw new ApiError(res.status, `POST ${path} failed: ${res.status}`);
  return (await res.json()) as KillSwitchResult;
}

// ---- Competition lifecycle (F-4 · create → register roster → start) ----
//
// The Create-Competition wizard's launch flow. All three are OWNER-scoped POSTs (auth-contract@1):
// the bearer is attached when the seam has a token (never fabricated) and the request retries once
// on a 401 (authedFetch). The backend derives the owner from the verified Privy principal — a
// client-supplied owner cannot reach it. Roster entries are INSTANCE-BOUND: each entry references a
// Studio-deployed instance via `instance_id`, and the arena runs the ACTUAL deployed contestant
// (pinned config_hash), never a same-named reconstruction. These bind `POST /competitions` →
// `POST /competitions/{id}/agents` (one entry per instance) → `POST /competitions/{id}/start`, NOT
// the separate strict intrinsic-arena endpoint (which rejects instance-bound entries).

/** The CompetitionConfig POST /competitions freezes (mirrors veridex CompetitionConfig, create subset). */
export interface CompetitionConfigPayload {
  competition_type: string;
  source_mode: 'replay' | 'live';
  execution_mode: ExecutionMode;
  market_scope: string;
  scoring_window: string | null;
  roster_size: number; // ge=2 (backend Field constraint) — the wizard guards this before firing
  // The AUTHORITATIVE catalog identity the Replay Library establishes (spec §5.2). The backend
  // competition model freezes a server-derived replay binding from these (models.py:83-84,106-127).
  // A label-only prefill loses this identity and breaks the moment a second admitted pack appears.
  pack_id?: string;
  fixture_id?: number;
}

/** One instance-bound roster entry POST /competitions/{id}/agents registers (mirrors AgentEntry). */
export interface RosterEntryPayload {
  agent_id: string;
  owner: string;
  strategy: string;
  model: string | null;
  proof_mode: string; // backend re-normalises to the two canonical values; sent advisory
  config_hash: string | null; // pins the referenced instance's config identity (I-7)
  execution_eligibility: boolean;
  instance_id: string | null; // the deployed-instance binding — the roster trust core
}

/** POST /competitions response (CompetitionCreateResponse). */
export interface CompetitionCreateResult { competition_id: string; status: string }
/** POST /competitions/{id}/agents response (AgentRegisterResponse). */
export interface AgentRegisterResult { agent_id: string; config_hash: string | null; proof_mode: string }
/** POST /competitions/{id}/start response (CompetitionStartResponse). */
export interface CompetitionStartResult { competition_id: string; status: string; run_id: string | null }

/** Create a DRAFT competition owned by the authenticated principal. Throws ApiError on non-ok. */
export async function createCompetition(config: CompetitionConfigPayload): Promise<CompetitionCreateResult> {
  const res = await authedFetch(PATHS.competitions(), config);
  if (!res.ok) throw new ApiError(res.status, `POST ${PATHS.competitions()} failed: ${res.status}`);
  return (await res.json()) as CompetitionCreateResult;
}

/**
 * Register ONE instance-bound roster entry. Throws ApiError carrying the real status so the launch
 * progression can distinguish a per-instance failure (403 not-owner / 404 absent / 409 roster
 * frozen-or-full / 400 domain) and offer retry-that-one or start-with-the-rest — never a fabricated
 * success. The backend fail-closes each of those before any roster mutation.
 */
export async function registerRosterAgent(
  competitionId: string, entry: RosterEntryPayload,
): Promise<AgentRegisterResult> {
  const res = await authedFetch(PATHS.competitionAgents(competitionId), entry);
  if (!res.ok) throw new ApiError(res.status, `POST ${PATHS.competitionAgents(competitionId)} failed: ${res.status}`);
  return (await res.json()) as AgentRegisterResult;
}

/**
 * Start the competition (freezes scoring law, source mode, roster, execution mode for the run) and
 * return the finalized handle. Throws ApiError on non-ok (404 unknown / 409 already started / 401/403
 * control-plane for non-paper / 501 live venue disabled) — the caller surfaces it, never fakes a run.
 */
export async function startCompetition(competitionId: string): Promise<CompetitionStartResult> {
  const res = await authedFetch(PATHS.competitionStart(competitionId), undefined);
  if (!res.ok) throw new ApiError(res.status, `POST ${PATHS.competitionStart(competitionId)} failed: ${res.status}`);
  return (await res.json()) as CompetitionStartResult;
}

// MOCK status-bar seed (sync): when mock is on, the status bar populates app-wide from the mock
// competition fixture (demoted source) so the full bar is inspectable — but WS is `disconnected`
// (DEMO), NEVER a fabricated CONNECTED. Returns null when mock is off (⇒ honest idle bar).
export function mockStatusSeed(): StatusBarState | null {
  if (!isMockEnabled()) return null;
  const c = adaptCompetitionState(MOCK_FIXTURES.competition);
  return {
    fixture: c.header.fixture,
    competition: c.header.competition,
    sourceMode: demote(c.header.source_mode),
    executionMode: c.header.execution_mode,
    ws: 'disconnected', // DEMO: no real stream in mock — honest, never CONNECTED
    seq: c.header.events ?? null,
    scoring: false,
    verifierVersion: c.header.verifier_version, // === the Proof Card's verifier (same artifact)
  };
}
