// T-2 remediation · the PUBLIC agent strategy profile deep-link must be HONEST off-mock: there is no
// agent-profile backend reader, so with the demo flag OFF the page must render an honest
// "unavailable" state and NEVER a fabricated AGENT_PROFILES record. With mock ON it serves the
// labeled DEMO fixture (or an honest not-found for an unknown id). useParams is mocked; nothing here
// touches the network (the ops drawer stays closed, so useRuntimeEvents never polls).
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

const h = vi.hoisted(() => ({ agentId: 'value_clv' }));
vi.mock('next/navigation', () => ({ useParams: () => ({ agentId: h.agentId }) }));

import AgentProfilePage from './page';

afterEach(() => {
  vi.unstubAllEnvs();
  h.agentId = 'value_clv';
});

const NAME = AGENT_PROFILES.value_clv.agent_name;

describe('AgentProfilePage deep-link honesty (T-2)', () => {
  it('mock OFF: renders an honest unavailable state — never the fabricated AGENT_PROFILES profile', async () => {
    h.agentId = 'value_clv';
    render(<AgentProfilePage />);
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    // the fabricated profile (its agent name / strategy caption) must NOT appear off-mock
    expect(screen.queryByRole('heading', { name: NAME })).toBeNull();
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
