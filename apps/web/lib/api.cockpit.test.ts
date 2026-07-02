import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getCockpitState } from '@/lib/api';
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
