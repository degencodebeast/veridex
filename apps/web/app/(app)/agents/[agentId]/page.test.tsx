// Quick honest enrichment · the PUBLIC agent strategy profile deep-link now resolves a REAL (leaner)
// profile off-mock via getAgentProfile (directional board + roster) — NO fabricated fixture. A seeded
// official renders its real aggregate stats; an unknown id is an honest "unavailable" state. With mock
// ON the labeled DEMO fixture is still served. useParams is mocked; fetch is stubbed for the off-mock
// reader fan-out (directional + roster), so nothing hits a real backend.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

const h = vi.hoisted(() => ({ agentId: 'value_clv' }));
vi.mock('next/navigation', () => ({ useParams: () => ({ agentId: h.agentId }) }));

import AgentProfilePage from './page';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  h.agentId = 'value_clv';
});

// The seeded official as it appears on the two real readers (see api.agent-profile.test.ts).
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
function routeFetch() {
  return vi.fn(async (url: unknown) => {
    const u = String(url);
    if (u.includes('/leaderboard/directional')) return new Response(JSON.stringify(DIRECTIONAL_WIRE), { status: 200 });
    if (u.includes('/agents/roster')) return new Response(JSON.stringify(ROSTER_WIRE), { status: 200 });
    throw new Error(`unexpected url ${u}`);
  });
}

const NAME = AGENT_PROFILES.value_clv.agent_name;

describe('AgentProfilePage deep-link (quick honest enrichment)', () => {
  it('mock OFF: a seeded official renders its REAL aggregate stats (name + real CLV)', async () => {
    vi.stubGlobal('fetch', routeFetch());
    h.agentId = 'agt_official_momentum';
    render(<AgentProfilePage />);
    expect(await screen.findByLabelText('Agent profile Official Momentum')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Official Momentum' })).toBeInTheDocument();
    expect(screen.getByText('+49.3 bps')).toBeInTheDocument(); // real avg CLV, never a fabricated value
  });

  it('mock OFF: an unknown id is an honest unavailable state — never a fabricated profile', async () => {
    vi.stubGlobal('fetch', routeFetch());
    h.agentId = 'agt_does_not_exist';
    render(<AgentProfilePage />);
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.queryByTestId('strategy-caption')).toBeNull();
  });

  it('mock ON: serves the labeled DEMO profile for a known id', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    h.agentId = 'value_clv';
    render(<AgentProfilePage />);
    expect(await screen.findByRole('heading', { name: NAME })).toBeInTheDocument();
    expect(screen.getByTestId('strategy-caption')).toBeInTheDocument();
  });

  it('mock ON: an unknown id is an honest not-found, not a fabricated profile', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    h.agentId = 'no_such_agent';
    render(<AgentProfilePage />);
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.queryByTestId('strategy-caption')).toBeNull();
  });
});
