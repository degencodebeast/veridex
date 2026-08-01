// Quick honest enrichment: getAgentProfile resolves a REAL (leaner) agent profile off-mock from the
// data we already serve (the directional public_agents board + the public roster) — NO new backend
// endpoint. Officials show real aggregate stats; fields we cannot fill degrade honestly (config_hash
// "—", empty competitions with breakdown_available:false, empty anchors). Mock ⇒ the AGENT_PROFILES
// fixture UNCHANGED. This mirrors the fetch-stub style of api.roster.test.ts / api.directional.test.ts.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { getAgentProfile } from '@/lib/api';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

// The seeded official `agt_official_momentum` as it appears on the two real readers (verified live:
// avg +49.3, total +1776, runs 2, valid_count 80, proof reproducible, unproven/not-eligible,
// none-anchored, all-replay). The directional row is a wire LeaderboardRow + display_name/public_agent_id.
const DIRECTIONAL_WIRE = {
  board_kind: 'public_agents',
  rows: [{
    rank: 1, agent_id: 'agt_official_momentum', runs: 2, avg_clv_bps: 49.3, total_clv_bps: 1776,
    sim_pnl: 0, brier: 0, max_drawdown: 0, action_count: 80, valid_pct: 100, proof_mode: 'reproducible',
    eligibility_badge: 'unproven', anchor_status: 'none-anchored', source_mode: 'all-replay',
    valid_count: 80, clv_confidence: 'high', low_sample: false,
    display_name: 'Official Momentum', public_agent_id: 'agt_official_momentum',
  }],
};

const ROSTER_WIRE = {
  agents: [{
    public_agent_id: 'agt_official_momentum', display_name: 'Official Momentum', owner_public_label: 'Veridex',
    origin: 'official', proof_state: 'reproducible', agent_id: 'inst_off_mom', type: 'momentum',
    source_mode: 'replay', execution_mode: 'paper', status: 'sealed', config_hash_present: true,
    avg_clv_bps: 49.3, runs: 2, valid_pct: 100,
  }],
};

// Route the two real reader fetches by URL: getAgentProfile fans out to getDirectionalLeaderboard
// (/leaderboard/directional?board_kind=public_agents) and getAgentsRoster (/agents/roster).
function routeFetch(directional: unknown, roster: unknown) {
  return vi.fn(async (url: unknown) => {
    const u = String(url);
    if (u.includes('/leaderboard/directional')) return new Response(JSON.stringify(directional), { status: 200 });
    if (u.includes('/agents/roster')) return new Response(JSON.stringify(roster), { status: 200 });
    throw new Error(`unexpected url ${u}`);
  });
}

describe('getAgentProfile — off-mock REAL profile from the directional board + roster', () => {
  it('maps a seeded official to its REAL aggregate stats, degrading honestly for absent fields', async () => {
    vi.stubGlobal('fetch', routeFetch(DIRECTIONAL_WIRE, ROSTER_WIRE));
    const p = await getAgentProfile('agt_official_momentum');
    expect(p).not.toBeNull();
    const profile = p!;
    // identity from the two readers
    expect(profile.agent_id).toBe('agt_official_momentum');
    expect(profile.agent_name).toBe('Official Momentum');
    expect(profile.archetype).toBe('momentum'); // from the roster type
    expect(profile.mode).toBeNull();            // roster carries no strategy mode
    // REAL aggregate stats from the board
    expect(profile.avg_clv_bps).toBe(49.3);
    expect(profile.total_clv_bps).toBe(1776);
    expect(profile.runs).toBe(2);
    expect(profile.valid_count).toBe(80);
    expect(profile.valid_pct).toBe(100);
    expect(profile.proof_mode).toBe('reproducible');
    expect(profile.eligibility_badge).toBe('not-eligible'); // unproven fails closed
    expect(profile.source_mode).toBe('replay');             // all-replay → replay
    expect(profile.source).toBe('STUDIO');                  // official ⇒ first-party classification
    // honest degradation for fields no endpoint exposes
    expect(profile.config_hash).toBe('—');
    expect(profile.policy_hash).toBe('—');
    expect(profile.completed_competitions).toEqual([]);
    expect(profile.anchors).toEqual([]);
    expect(profile.breakdown_available).toBe(false);
    // real deployment provenance from the roster identity
    expect(profile.deployment_provenance).toContain('Veridex');
    expect(profile.deployment_provenance).toContain('official');
    // strategy caption is an honest "not exposed" note, never a fabricated strategy
    expect(profile.strategy_caption).toMatch(/not exposed/i);
  });

  // Provenance honesty: `source` is classified by the roster's real `origin` ALONE. proof_mode is
  // orthogonal to authorship (and coerces mixed/unknown/'' → reproducible), so a third-party BYOA agent
  // with a reproducible board proof_mode must STILL be BYOA — never falsely stamped first-party STUDIO.
  it('classifies a third-party agent (roster origin "byoa") as BYOA, never STUDIO from proof_mode', async () => {
    const byoaDirectional = {
      board_kind: 'public_agents',
      rows: [{ ...DIRECTIONAL_WIRE.rows[0], proof_mode: 'reproducible',
        agent_id: 'agt_byoa_alpha', public_agent_id: 'agt_byoa_alpha', display_name: 'BYOA Alpha' }],
    };
    const byoaRoster = {
      agents: [{ ...ROSTER_WIRE.agents[0], public_agent_id: 'agt_byoa_alpha', display_name: 'BYOA Alpha',
        origin: 'byoa', type: 'value_clv', proof_state: 'reproducible' }],
    };
    vi.stubGlobal('fetch', routeFetch(byoaDirectional, byoaRoster));
    const p = await getAgentProfile('agt_byoa_alpha');
    expect(p?.source).toBe('BYOA'); // origin byoa ⇒ BYOA, despite a reproducible proof_mode
  });

  // Roster-absent edge (e.g. the roster fetch fails → honest-empty [] while the board succeeds): identity
  // fields degrade honestly and NEVER seed a specific strategy — archetype is the "—" absent marker,
  // mode null, provenance the "not exposed" note, and source the least-claim BYOA.
  it('roster-absent identity degrades honestly (archetype "—", no seeded strategy), source BYOA', async () => {
    vi.stubGlobal('fetch', routeFetch(DIRECTIONAL_WIRE, { agents: [] }));
    const p = await getAgentProfile('agt_official_momentum');
    expect(p).not.toBeNull();
    expect(p!.archetype).toBe('—');     // never a fabricated 'baseline' / specific archetype
    expect(p!.mode).toBeNull();
    expect(p!.source).toBe('BYOA');      // no roster origin ⇒ least-claim, never a fabricated STUDIO
    expect(p!.deployment_provenance).toMatch(/not exposed/i);
    // the REAL board stats still render — the profile is not withheld
    expect(p!.avg_clv_bps).toBe(49.3);
  });

  it('an unknown id is an honest not-found (null), never a fabricated profile', async () => {
    vi.stubGlobal('fetch', routeFetch(DIRECTIONAL_WIRE, ROSTER_WIRE));
    expect(await getAgentProfile('agt_does_not_exist')).toBeNull();
  });

  it('any transport error → null (honest-unavailable, never a fixture off-mock)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
    expect(await getAgentProfile('agt_official_momentum')).toBeNull();
  });

  it('mock mode returns the AGENT_PROFILES fixture UNCHANGED (preserves today\'s behavior)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    expect(await getAgentProfile('value_clv')).toBe(AGENT_PROFILES.value_clv);
    expect(await getAgentProfile('no_such_agent')).toBeNull();
  });
});
