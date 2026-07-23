// E3 · The /leaderboard directional board is wired to the REAL public board off-mock
// (GET /leaderboard/directional) through getDirectionalLeaderboard(): it renders honest DISPLAY NAMES
// (never opaque public ids), survives the REPLAY filter (all-replay provenance, M8), and honest-empties
// on a fetch error (NEVER a wire fixture). Mock ON is UNCHANGED (still the getLeaderboard() fixture path).
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const getDirectionalLeaderboardMock = vi.fn();
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getDirectionalLeaderboard: () => getDirectionalLeaderboardMock() };
});

import LeaderboardPage from './page';
import type { DirectionalRow } from '@/lib/catalog';

const ROWS: DirectionalRow[] = [
  {
    rank: 1, agent_id: 'pa_1', public_agent_id: 'pa_1', display_name: 'Falcon Value', agent_name: 'Falcon Value',
    agent_kind: '', runs: 5, avg_clv_bps: 20, total_clv_bps: 100, sim_pnl: 1, brier: 0.2, max_drawdown: -2,
    action_count: 40, valid_pct: 90, proof_mode: 'reproducible', eligibility_badge: 'eligible',
    anchor_status: 'anchored', source_mode: 'replay', valid_count: 40, clv_confidence: 'high', low_sample: false,
  },
  {
    rank: 2, agent_id: 'pa_2', public_agent_id: 'pa_2', display_name: 'Otter Momentum', agent_name: 'Otter Momentum',
    agent_kind: '', runs: 3, avg_clv_bps: 12, total_clv_bps: 36, sim_pnl: 0.5, brier: 0.22, max_drawdown: -1,
    action_count: 20, valid_pct: 80, proof_mode: 'partial', eligibility_badge: 'not-eligible',
    anchor_status: 'pending', source_mode: 'replay', valid_count: 20, clv_confidence: 'medium', low_sample: false,
  },
];

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/');
});

describe('LeaderboardPage — off-mock DIRECTIONAL board (E3)', () => {
  it('off-mock: renders REAL display names from /leaderboard/directional (never opaque ids)', async () => {
    getDirectionalLeaderboardMock.mockResolvedValue(ROWS);
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(2));
    expect(screen.getByText(/Falcon Value/)).toBeInTheDocument();
    expect(screen.getByText(/Otter Momentum/)).toBeInTheDocument();
    // the opaque public id is NOT the visible label
    expect(screen.queryByText('pa_1')).toBeNull();
    expect(getDirectionalLeaderboardMock).toHaveBeenCalledTimes(1);
  });

  it('survives the REPLAY filter (all-replay provenance, M8)', async () => {
    getDirectionalLeaderboardMock.mockResolvedValue(ROWS);
    const user = userEvent.setup();
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(2));
    await user.click(screen.getByRole('radio', { name: 'REPLAY' }));
    expect(screen.getAllByTestId('lb-row').length).toBe(2);
    expect(screen.getByText(/Falcon Value/)).toBeInTheDocument();
  });

  it('off-mock error → honest-empty, NEVER a wire fixture', async () => {
    getDirectionalLeaderboardMock.mockRejectedValue(new Error('offline'));
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getByTestId('lb-empty')).toBeInTheDocument());
    expect(screen.queryByTestId('lb-row')).toBeNull();
  });

  it('mock ON is UNCHANGED: does NOT call the directional reader', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<LeaderboardPage />);
    // mock ON serves the canonical wire fixture via getLeaderboard() — rows appear …
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBeGreaterThan(0));
    // … and the directional reader is never touched.
    expect(getDirectionalLeaderboardMock).not.toHaveBeenCalled();
  });
});
