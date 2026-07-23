import { describe, it, expect, vi, afterEach } from 'vitest';
import { getAgentsRoster } from '@/lib/api';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

// An UNSCORED public roster row: proof_state "unscored", performance columns null, origin "official".
const UNSCORED_WIRE = {
  agents: [{
    public_agent_id: 'pa_official_01', display_name: 'Official Baseline', owner_public_label: '—',
    origin: 'official', proof_state: 'unscored', agent_id: 'inst_abc', type: 'baseline',
    source_mode: 'replay', execution_mode: 'paper', status: 'sealed', config_hash_present: true,
    avg_clv_bps: null, runs: null, valid_pct: null,
  }],
};

// A SCORED public roster row: a real proof_state + numeric performance columns (pooled values).
const SCORED_WIRE = {
  agents: [{
    public_agent_id: 'pa_value_01', display_name: 'Value CLV', owner_public_label: 'acme',
    origin: 'byoa', proof_state: 'reproducible', agent_id: 'inst_xyz', type: 'value_clv',
    source_mode: 'live', execution_mode: 'live_guarded', status: 'sealed', config_hash_present: true,
    avg_clv_bps: 18.4, runs: 14, valid_pct: 95.0,
  }],
};

describe('getAgentsRoster → PublicAgentRow[] (honest identity + honest proof-state)', () => {
  it('maps an UNSCORED wire row → proof_state "unscored", origin preserved, perf null, NO source field', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(UNSCORED_WIRE), { status: 200 })));
    const rows = await getAgentsRoster();
    expect(rows).toHaveLength(1);
    const r = rows[0];
    expect(r.public_agent_id).toBe('pa_official_01');
    expect(r.display_name).toBe('Official Baseline');
    expect(r.owner_public_label).toBe('—');
    expect(r.origin).toBe('official');
    expect(r.proof_state).toBe('unscored');
    expect(r.avg_clv_bps).toBeNull();
    expect(r.runs).toBeNull();
    expect(r.valid_pct).toBeNull();
    expect(r).not.toHaveProperty('source'); // PublicAgentRow carries NO STUDIO/BYOA source field
  });

  it('maps a SCORED wire row → real proof_state + numeric perf', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(SCORED_WIRE), { status: 200 })));
    const rows = await getAgentsRoster();
    const r = rows[0];
    expect(r.proof_state).toBe('reproducible');
    expect(r.avg_clv_bps).toBe(18.4);
    expect(r.runs).toBe(14);
    expect(r.valid_pct).toBe(95.0);
    expect(r.archetype).toBe('value_clv'); // template_id preserved as archetype
    expect(r.owner_public_label).toBe('acme');
    expect(r.origin).toBe('byoa');
  });

  it('honest-empty [] on a fetch error — NEVER the AGENTS fixture off-mock (T-2)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
    expect(await getAgentsRoster()).toEqual([]);
  });
});
