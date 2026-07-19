import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  getProofArtifact, getCockpitState, getInspectorRecord, getLeaderboard, verifyProof,
  ApiError, adaptProofArtifact, adaptVerify, adaptLeaderboard,
} from '@/lib/api';
import { CHECK_ORDER } from '@/lib/checks';
import type * as W from '@/lib/wire';

const FIX = resolve(__dirname, '../../../contracts/fixtures');
function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIX, name), 'utf8')) as T;
}

const proofArtifactWire = fixture<W.ProofArtifact>('proof_artifact.json');
const verifyWire = fixture<W.VerifyResult>('verify_response.json');
const competitionWire = fixture<W.CompetitionStateResponse>('competition_state.json');
const leaderboardWire = fixture<W.LeaderboardResponse>('leaderboard.json');
const inspectorWire = fixture<W.InspectorRecord>('inspector_record.json');

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

function stubFetch(impl: typeof fetch) {
  vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch);
}
function calls() {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
}

describe('api client (CON-003: binds the frozen wire contract, maps to the view-model)', () => {
  it('GETs the run proof route and maps wire ProofArtifact → view-model (7 checks, no CLV in checks)', async () => {
    stubFetch(async () => new Response(JSON.stringify(proofArtifactWire), { status: 200 }));
    const a = await getProofArtifact('run_7f3a');
    expect(a.checks).toHaveLength(7);
    expect(a.checks.map((c) => c.id).sort()).toEqual([...CHECK_ORDER].sort());
    expect(a.checks.some((c) => /clv/i.test(c.id) || /clv/i.test(c.label))).toBe(false);
    expect(typeof a.metrics.clv_bps).toBe('number');
    expect(a.metrics.clv_bps).toBe(92); // CLV lives in metrics, mapped faithfully
    // EXACT path: GET /runs/{id} (backend serves the ProofArtifact here). A regression
    // re-adding the old /proof suffix would 404 against the real API — so pin it precisely.
    const proofUrl = String(calls()[0][0]);
    expect(proofUrl).toMatch(/\/runs\/run_7f3a$/);
    expect(proofUrl).not.toContain('/proof');
  });

  it('POSTs the authoritative verify endpoint and preserves verified + the 7 checks (WD-1)', async () => {
    stubFetch(async () => new Response(JSON.stringify(verifyWire), { status: 200 }));
    const r = await verifyProof('run_7f3a');
    expect(r.verified).toBe(true);
    expect(r.ok).toBe(true);
    expect(r.evidence_hash_confirmed).toBe(true); // evidence_hash === recomputed_evidence_hash in fixture
    // manifest_bound=pass in the fixture (backend now binds the manifest) → confirmed, not false-red.
    expect(r.manifest_hash_confirmed).toBe(true);
    expect(r.checks).toHaveLength(7);
    expect(r.checks.some((c) => /clv/i.test(c.id))).toBe(false);
    const [url, init] = calls()[0];
    expect(String(url)).toContain('/runs/run_7f3a/verify');
    expect((init as RequestInit).method).toBe('POST');
  });

  it('GETs the leaderboard and preserves the WD-7 confidence fields onto the view-model', async () => {
    stubFetch(async () => new Response(JSON.stringify(leaderboardWire), { status: 200 }));
    const rows = await getLeaderboard();
    expect(rows.length).toBeGreaterThan(0);
    const r = rows[0];
    expect(typeof r.valid_count).toBe('number');
    expect(typeof r.clv_confidence).toBe('string');
    expect(typeof r.low_sample).toBe('boolean');
    // WD-7 is display-only and never reorders (SEC-005): rank preserved as given.
    expect(r.rank).toBe(1);
  });

  it('preserves a null cross-run avg_clv_bps (UNSCORED, action_count==0) as null — never a fabricated 0 bps (R-globalclv)', () => {
    // Backend contract: cross-run LeaderboardRow.avg_clv_bps is `float | None`, None when
    // action_count == 0 (veridex/api/schemas.py:44,63). An UNSCORED agent must stay "unavailable"
    // (rendered "—"), never read as an earned 0 bps. Mirrors the F-5 competition-board honesty fix.
    const wire = {
      rows: [
        {
          rank: 1, agent_id: 'scored', runs: 3, avg_clv_bps: 14.2, total_clv_bps: 42, sim_pnl: 42,
          brier: null, max_drawdown: 0, action_count: 3, valid_pct: 100, proof_mode: 'reproducible',
          eligibility_badge: 'unproven', anchor_status: 'none-anchored', source_mode: 'all-replay',
          valid_count: 3, clv_confidence: 'low', low_sample: true,
        },
        {
          rank: 2, agent_id: 'unscored', runs: 0, avg_clv_bps: null, total_clv_bps: 0, sim_pnl: 0,
          brier: null, max_drawdown: 0, action_count: 0, valid_pct: 0, proof_mode: 'reproducible',
          eligibility_badge: 'unproven', anchor_status: 'none-anchored', source_mode: 'all-replay',
          valid_count: 0, clv_confidence: 'low', low_sample: true,
        },
      ],
    } as unknown as W.LeaderboardResponse;
    const rows = adaptLeaderboard(wire);
    // UNSCORED row: null preserved (RED pre-fix: the `?? 0` coercion turned this into 0).
    expect(rows[1].avg_clv_bps).toBeNull();
    // SCORED row: real bps carried through unchanged (no regression on always-numeric rows).
    expect(rows[0].avg_clv_bps).toBe(14.2);
    // Backend rank + row order preserved verbatim — the adapter introduces no local re-sort.
    expect(rows.map((r) => [r.agent_id, r.rank])).toEqual([['scored', 1], ['unscored', 2]]);
  });

  it('GETs competition state from /competitions/{id}', async () => {
    stubFetch(async () => new Response(JSON.stringify(competitionWire), { status: 200 }));
    // Derive the id from the fixture so this never drifts when it is regenerated.
    const id = competitionWire.competition_id;
    const s = await getCockpitState(id);
    expect(s.competition_id).toBe(id);
    expect(String(calls()[0][0])).toContain(`/competitions/${id}`);
  });

  it('GETs the inspector record and maps wire InspectorRecord → view-model', async () => {
    stubFetch(async () => new Response(JSON.stringify(inspectorWire), { status: 200 }));
    const rec = await getInspectorRecord('0ccc120de9024314a5a890e9fa34c370', 0);
    expect(rec.action_seq).toBe(0); // wire tick_seq
    expect(rec.agent_action.type).toBeTruthy();
    expect(rec.untrusted_llm).not.toBeNull();
    expect(String(calls()[0][0])).toContain('/runs/0ccc120de9024314a5a890e9fa34c370');
  });

  it('adaptProofArtifact preserves all 7 check statuses — a failing check is not dropped', () => {
    const failing: W.ProofArtifact = {
      ...proofArtifactWire,
      checks: {
        ...proofArtifactWire.checks,
        evidence_integrity: { ...proofArtifactWire.checks.evidence_integrity, result: 'fail' },
      },
    };
    const a = adaptProofArtifact(failing);
    expect(a.checks).toHaveLength(7);
    const evid = a.checks.find((c) => c.id === 'evidence_integrity');
    expect(evid?.result).toBe('fail'); // preserved, not dropped or coerced to pass
    expect(a.checks.find((c) => c.id === 'anchor')?.result).toBe('not_applicable');
  });

  it('adaptVerify maps wire verified=false through faithfully', () => {
    const v = adaptVerify({ ...verifyWire, verified: false });
    expect(v.verified).toBe(false);
    expect(v.ok).toBe(false);
  });

  it('throws ApiError on a non-2xx response', async () => {
    stubFetch(async () => new Response('nope', { status: 500 }));
    await expect(getProofArtifact('bad')).rejects.toBeInstanceOf(ApiError);
  });
});
