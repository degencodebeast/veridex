import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getCockpitState, adaptCompetitionState } from '@/lib/api';
import type * as W from '@/lib/wire';

// Foundation: a fixture-seeded REPLAY cockpit projection. Under mock the reader returns a POPULATED
// demo CockpitState (trace/match/leaderboard/events/receipts/policy); in live it stays honest-empty
// (the real WS fills it). REPLAY labeled REPLAY — never LIVE. Match phase (in-play) is a separate axis.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
const competitionWire = JSON.parse(readFileSync(resolve(FIX, 'competition_state.json'), 'utf8')) as W.CompetitionStateResponse;

function stubFetch(impl: typeof fetch) { vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch); }

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('cockpit projection (fixture-seeded REPLAY demo, mock-gated)', () => {
  it('MOCK: getCockpitState returns a POPULATED demo projection — REPLAY source, never LIVE', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const s = await getCockpitState('wc-fra-bra');
    // populated body (the demo projection the Cockpit screen renders)
    expect(s.trace.length).toBeGreaterThan(0);
    expect(s.events.length).toBeGreaterThan(0);
    expect(s.leaderboard.length).toBeGreaterThan(0);
    expect(s.receipts.length).toBeGreaterThan(0);
    expect(s.policy.length).toBeGreaterThan(0);
    // honesty: the source axis is REPLAY (demoted), never LIVE, even though the MATCH phase is in-play
    expect(s.header.source_mode).toBe('replay');
    expect(s.match.status).toBe('live'); // match phase (a fixture fact) — separate axis from source_mode
    // canonical event stream carries the sealed-evidence flag (some true, some derived-false)
    expect(s.events.some((e) => e.evidence === true)).toBe(true);
  });

  it('LIVE (mock off): getCockpitState is honest-empty until the WS fills it (no fabricated projection)', async () => {
    stubFetch(async () => new Response(JSON.stringify(competitionWire), { status: 200 }));
    const s = await getCockpitState(competitionWire.competition_id);
    expect(s.events.length).toBe(0);
    expect(s.trace.length).toBe(0);
    expect(s.receipts.length).toBe(0);
  });
});

// F-5: the cockpit centerpiece — leaderboard + receipts — must populate in LIVE from the REAL
// competition-scoped backend (GET /competitions/{id}), honestly and backend-authoritatively.
describe('F-5 · adaptCompetitionState binds the competition-scoped leaderboard + receipts', () => {
  // A sparse competition-scoped wire row (CompetitionLeaderboardRow) — SMALLER than the cross-run
  // LeaderboardRow: no sim_pnl/brier/max_drawdown/action_count/valid_pct/agent_name.
  const wireWith = (over: Partial<W.CompetitionStateResponse>): W.CompetitionStateResponse => ({
    competition_id: 'comp-x', status: 'running',
    config: { source_mode: 'live', execution_mode: 'paper' }, roster: [],
    leaderboard: [], latest_seq: 5, anchor_status: 'pending', run_id: 'run-x',
    proof_card: null, execution: null, ...over,
  });

  it('maps the sparse competition leaderboard into rendered rows (populates — no longer stuck [])', () => {
    const s = adaptCompetitionState(wireWith({
      leaderboard: [
        { rank: 1, agent_id: 'agent-alpha', total_clv_bps: 184, mean_clv_bps: 92, valid_count: 2, proof_mode: 'reproducible' },
        { rank: 2, agent_id: 'agent-beta', total_clv_bps: -184, mean_clv_bps: -92, valid_count: 2, proof_mode: 'reproducible' },
      ] as unknown as Record<string, unknown>[],
    }));
    expect(s.leaderboard.length).toBe(2);
    expect(s.leaderboard[0].agent_id).toBe('agent-alpha');
    expect(s.leaderboard[0].avg_clv_bps).toBe(92); // mean_clv_bps → avg_clv_bps
    expect(s.leaderboard[0].valid_count).toBe(2);
  });

  it('preserves the BACKEND rank + order verbatim — never a local CLV re-sort (CON-203)', () => {
    // Backend order deliberately DISAGREES with avg-CLV-desc: rank 1 has the LOWER mean.
    const s = adaptCompetitionState(wireWith({
      leaderboard: [
        { rank: 1, agent_id: 'low', total_clv_bps: 10, mean_clv_bps: 5, valid_count: 1, proof_mode: 'reproducible' },
        { rank: 2, agent_id: 'high', total_clv_bps: 40, mean_clv_bps: 40, valid_count: 1, proof_mode: 'reproducible' },
      ] as unknown as Record<string, unknown>[],
    }));
    expect(s.leaderboard.map((r) => r.agent_id)).toEqual(['low', 'high']); // verbatim order
    expect(s.leaderboard.map((r) => r.rank)).toEqual([1, 2]);              // backend rank
  });

  it('marks competition-absent proxy metrics null (honest "—"), never a fabricated 0', () => {
    const s = adaptCompetitionState(wireWith({
      leaderboard: [{ rank: 1, agent_id: 'a', total_clv_bps: 1, mean_clv_bps: 1, valid_count: 1, proof_mode: 'verified' }] as unknown as Record<string, unknown>[],
    }));
    const row = s.leaderboard[0];
    expect(row.sim_pnl).toBeNull();
    expect(row.brier).toBeNull();
    expect(row.max_drawdown).toBeNull();
    expect(row.action_count).toBeNull();
    expect(row.valid_pct).toBeNull();
  });

  it('projects receipts from the REAL execution attachment (sealed decision receipts, not fixtures)', () => {
    const s = adaptCompetitionState(wireWith({
      execution: {
        non_scoring: true, derived: true, venue_artifact: true,
        receipts: [{
          execution_id: 'exec-1', venue: 'sxbet', market_ref: '1X2:FRA', side: 'back',
          requested_size: 100, filled_size: 100, price: 1.472, status: 'filled',
          venue_order_id: 'ord-1', mode: 'paper',
          submitted_at: '2026-07-17T12:00:00Z', settled_at: null,
        }],
      } as unknown as Record<string, unknown>,
    }));
    expect(s.receipts.length).toBe(1);
    expect(s.receipts[0].execution_id).toBe('exec-1');
    expect(s.receipts[0].status).toBe('filled');
    expect(s.receipts[0].submitted_at).not.toBeNull(); // ISO string → epoch seconds (presence kept)
    expect(s.receipts[0].settled_at).toBeNull();        // honest null preserved
  });

  it('T-2 honest-empty: no leaderboard rows + null execution ⇒ empty, never a fixture', () => {
    const s = adaptCompetitionState(wireWith({ leaderboard: [], execution: null }));
    expect(s.leaderboard).toEqual([]);
    expect(s.receipts).toEqual([]);
  });
});

// F-5 · RED-3: the cockpit fetch must hit the COMPETITION-SCOPED path, never the global cross-run
// /leaderboard (which ignores competition_id → wrong board, v2.10.10 §A1).
describe('F-5 · getCockpitState uses the competition-scoped path, never the global /leaderboard', () => {
  it('LIVE: fetches GET /competitions/{id}, populates the board from it, and never calls /leaderboard', async () => {
    const urls: string[] = [];
    stubFetch(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(JSON.stringify(competitionWire), { status: 200 });
    });
    const s = await getCockpitState(competitionWire.competition_id);
    expect(urls.some((u) => u.includes(`/competitions/${competitionWire.competition_id}`))).toBe(true);
    // The global cross-run endpoint (bare /leaderboard, with or without a competition_id query) must
    // NEVER back the competition board.
    expect(urls.some((u) => /\/leaderboard(\?|$)/.test(u))).toBe(false);
    expect(s.leaderboard.length).toBeGreaterThan(0); // populated from the competition-scoped fetch
  });
});
