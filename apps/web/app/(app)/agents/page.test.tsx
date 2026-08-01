// T-2 remediation · The /agents primary-nav tab must NOT show a fabricated DIRECTIONAL roster with
// the demo flag OFF. There is NO agents-list backend reader/endpoint (lib/api.ts exposes none), so the
// directional roster surfaces ONLY under the mock gate (demo); off-mock it is honest-empty — a judge on
// /agents with the flag OFF sees "no agents yet", NEVER the AGENTS fixture.
//
// The mock flag is driven by NEXT_PUBLIC_VERIDEX_MOCK (isMockEnabled reads it) so the directional roster
// gates exactly as in the app. This exercises the PAGE's wiring, where the mock gate lives.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import AgentsPage from './page';
import { AGENTS } from '@/lib/fixtures/catalog';

afterEach(() => {
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
});

describe('AgentsPage — directional roster is mock-gated (T-2, honest off-mock)', () => {
  it('mock OFF: renders honest-empty directional roster, NEVER the AGENTS fixture', async () => {
    render(<AgentsPage />);
    // the honest-empty directional state is shown …
    await waitFor(() => expect(screen.getByTestId('agents-empty')).toBeInTheDocument());
    // … and NONE of the fabricated agents leak: no /agents/:id row link, no signature names.
    const agentRowLinks = screen
      .queryAllByRole('link')
      .filter((l) => l.getAttribute('href')?.startsWith('/agents/'));
    expect(agentRowLinks).toHaveLength(0);
    expect(screen.queryByText(/Value CLV/)).toBeNull();
    expect(screen.queryByText(/Momentum FR/)).toBeNull();
  });

  it('mock ON: the demo AGENTS roster surfaces — every fixture agent renders a profile link', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<AgentsPage />);
    await waitFor(() =>
      expect(screen.getByRole('link', { name: /Value CLV/i })).toHaveAttribute('href', '/agents/value_clv'),
    );
    const agentRowLinks = screen
      .queryAllByRole('link')
      .filter((l) => l.getAttribute('href')?.startsWith('/agents/'));
    expect(agentRowLinks).toHaveLength(AGENTS.length);
    expect(screen.queryByTestId('agents-empty')).toBeNull();
  });
});
