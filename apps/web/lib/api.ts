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
import { isMockEnabled, MOCK_FIXTURES } from '@/lib/mock';
import { COCKPIT_DEMO } from '@/lib/fixtures/cockpit';
import { INSPECTOR_DEMO_QUANTITIES } from '@/lib/fixtures/inspector';
import { PROOF_DEMO_ROOTS, mapRootForest } from '@/lib/fixtures/proof';
import { PROOF_EXPLAIN_DEMO, type ProofExplanation } from '@/lib/explainer';
import { VERIFIER_VERSION, type StatusBarState } from '@/lib/status';
import type * as W from '@/lib/wire';
import type {
  AnchorInfo, AnchorStatus, CheckResult, CockpitState, ExecutionMode, FeedHealthState,
  InspectorRecord, LeaderboardRow, MatchState, PerformanceMetrics, ProofArtifact, ProofMode,
  SourceMode, VerifyResult,
} from '@/lib/contracts';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '';

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
  // C2 catalog: agent runtime-events (OPS telemetry; ImplD-served orphaned route).
  runtimeEvents: (agentId: string) => `/agents/${agentId}/runtime-events`,
} as const;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: { accept: 'application/json' } });
  if (!res.ok) throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, `POST ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

// ---- pure coercion helpers ----
function toAnchorStatus(s: string): AnchorStatus {
  if (s === 'anchored' || s === 'pending' || s === 'not_applicable') return s;
  return 'not-anchored';
}
function toProofMode(s: string): ProofMode {
  return s === 'verified' || s === 'partial' ? s : 'reproducible';
}
function toSourceMode(s: string): SourceMode {
  return s === 'live' ? 'live' : 'replay';
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
    avg_clv_bps: r.avg_clv_bps ?? 0,
    total_clv_bps: r.total_clv_bps,
    sim_pnl: r.sim_pnl,
    brier: r.brier ?? 0,
    max_drawdown: r.max_drawdown,
    action_count: r.action_count,
    valid_pct: r.valid_pct, // PERCENT 0-100, passed through 1:1 from the wire
    proof_mode: toProofMode(r.proof_mode),
    eligibility_badge: r.eligibility_badge === 'eligible' ? 'eligible' : 'not-eligible',
    anchor_status: toAnchorStatus(r.anchor_status),
    source_mode: r.source_mode === 'live' ? 'live' : r.source_mode === 'replay' ? 'replay' : 'mixed',
    // WD-7 confidence preserved faithfully (display-only, never reorders — SEC-005).
    valid_count: r.valid_count,
    clv_confidence: r.clv_confidence,
    low_sample: r.low_sample,
  }));
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
    // GAPs: the cockpit's trace/match/events/receipts/policy/kill_armed and the rich
    // leaderboard are NOT in GET /competitions/{id}; the cockpit screen assembles them
    // from the WS stream (useArenaStream) + GET /leaderboard.
    trace: [],
    match: emptyMatch(),
    leaderboard: [],
    events: [],
    receipts: [],
    policy: [],
    kill_armed: false,
  };
}

export function adaptInspector(w: W.InspectorRecord): InspectorRecord {
  const rec = w.recompute as { recomputed_edge_bps?: number; clv_bps?: number; valid?: boolean };
  const llm = w.untrusted_llm_metadata as { reason?: string; confidence?: number; claimed_edge_bps?: number; model?: string };
  const clv = typeof w.clv_bps === 'number' ? w.clv_bps : Number(w.clv_bps) || 0;
  return {
    run_id: w.run_id,
    agent_id: w.agent_id,
    action_seq: w.tick_seq,
    proof_mode: 'reproducible', // GAP: not in wire InspectorRecord
    is_live: false, // GAP
    market_state: w.market_state as unknown as InspectorRecord['market_state'],
    agent_action: w.agent_action as unknown as InspectorRecord['agent_action'],
    recompute: { recomputed_edge_bps: rec.recomputed_edge_bps ?? 0, clv_bps: rec.clv_bps ?? 0, valid: rec.valid ?? false },
    // GAP: the wire InspectorRecord carries no doctrine quantities (fair value,
    // executable edge, venue price, stake) → null = honest "not in proof artifact"
    // (rendered as "—"), NOT a plausible 0. CLV (the real score) is carried through.
    clv_explanation: {
      fair_value_pct: null, closing_fair_value_pct: null, venue_decimal_price: null,
      executable_edge_bps: null, clv_bps: clv, stake_fraction: null,
      plain: '',
    },
    untrusted_llm: llm
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
