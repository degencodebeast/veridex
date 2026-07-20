// T-2 remediation · The /duel DIRECTIONAL duel must NOT show a fabricated head-to-head with the demo
// flag OFF. There is NO agents/duel backend reader/endpoint (lib/api.ts exposes none), so the directional
// agents surface ONLY under the mock gate (demo); off-mock the directional lane is honest-empty — a judge
// on /duel with the flag OFF sees the "select at least two agents" empty state, NEVER the AGENTS fixture.
//
// The mock flag is driven by NEXT_PUBLIC_VERIDEX_MOCK (isMockEnabled reads it) so the directional agents
// gate exactly as in the app. This exercises the PAGE's wiring, where the mock gate lives. The Maker lane
// (F-9) is a SEPARATE population sourced from its own fixture and is out of scope here.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import DuelPage from './page';

afterEach(() => {
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
});

describe('DuelPage — directional duel is mock-gated (T-2, honest off-mock)', () => {
  it('mock OFF: renders honest-empty directional duel, NEVER the AGENTS fixture', async () => {
    render(<DuelPage />);
    // the honest-empty directional state is shown (fewer than two agents) …
    await waitFor(() => expect(screen.getByTestId('duel-empty')).toBeInTheDocument());
    // … and NONE of the fabricated agents leak: no agent picker, no signature names, no duel cards.
    expect(screen.queryByLabelText(/agent a/i)).toBeNull();
    expect(screen.queryAllByTestId('duel-card')).toHaveLength(0);
    expect(screen.queryByText(/Value CLV/)).toBeNull();
    expect(screen.queryByText(/Momentum FR/)).toBeNull();
  });

  it('mock ON: the demo AGENTS roster surfaces — the head-to-head picker and cards render', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<DuelPage />);
    await waitFor(() => expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument());
    expect(screen.getAllByTestId('duel-card')).toHaveLength(2);
    expect(screen.queryByTestId('duel-empty')).toBeNull();
  });
});
