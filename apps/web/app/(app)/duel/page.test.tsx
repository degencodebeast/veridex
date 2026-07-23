// E4 · The /duel Public-Agents lane sources its rows off-mock. The PAGE owns ONLY the mock gate
// (resolved in an effect, hydration-safe): mock ON → the labeled DEMO AGENTS fixture mapped through the
// SHARED agentSummaryToPublicRow adapter; mock OFF → mockAgents=null, and the SCREEN does the single
// real getAgentsRoster() read. A judge on /duel with the flag OFF sees the REAL public roster (or an
// honest-empty compare), NEVER the AGENTS fixture. The Maker lane is a SEPARATE population out of scope.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { getAgentsRoster } from '@/lib/api';

import DuelPage from './page';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getAgentsRoster: vi.fn(async () => []),
  getMakerArenaResult: vi.fn(),
}));

const getAgentsRosterMock = vi.mocked(getAgentsRoster);

afterEach(() => {
  vi.unstubAllEnvs();
  getAgentsRosterMock.mockReset();
  getAgentsRosterMock.mockResolvedValue([]);
  window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
});

describe('DuelPage — Public Agents lane is mock-gated; off-mock reads the real roster (E4)', () => {
  it('mock OFF: the SCREEN performs the single real getAgentsRoster read (never the AGENTS fixture)', async () => {
    getAgentsRosterMock.mockResolvedValue([
      { public_agent_id: 'pa_real_a', display_name: 'Real Alpha', owner_public_label: '—', origin: 'official', proof_state: 'unscored', archetype: 'baseline', mode: null, avg_clv_bps: null, runs: null, valid_pct: null },
      { public_agent_id: 'pa_real_b', display_name: 'Real Beta', owner_public_label: 'acme', origin: 'byoa', proof_state: 'reproducible', archetype: 'value_clv', mode: null, avg_clv_bps: 12.5, runs: 9, valid_pct: 90 },
    ]);
    render(<DuelPage />);
    await waitFor(() => expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument());
    expect(getAgentsRosterMock).toHaveBeenCalledTimes(1);
    // the display name appears in both the <option> and the card heading → at least one occurrence
    expect(screen.getAllByText('Real Alpha').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Real Beta').length).toBeGreaterThan(0);
    // the fabricated demo names never leak off-mock
    expect(screen.queryByText(/Momentum FR/)).toBeNull();
  });

  it('mock OFF, empty roster: honest-empty compare, NEVER the AGENTS fixture', async () => {
    getAgentsRosterMock.mockResolvedValue([]);
    render(<DuelPage />);
    await waitFor(() => expect(screen.getByTestId('duel-empty')).toBeInTheDocument());
    expect(screen.queryByLabelText(/agent a/i)).toBeNull();
    expect(screen.queryAllByTestId('duel-card')).toHaveLength(0);
    expect(screen.queryByText(/Value CLV/)).toBeNull();
  });

  it('mock ON: the demo AGENTS roster surfaces via the shared adapter — NO real getAgentsRoster read', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<DuelPage />);
    await waitFor(() => expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument());
    expect(screen.getAllByTestId('duel-card')).toHaveLength(2);
    expect(screen.queryByTestId('duel-empty')).toBeNull();
    // injected mock rows ⇒ the screen must NOT fall through to the real reader
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
  });
});
