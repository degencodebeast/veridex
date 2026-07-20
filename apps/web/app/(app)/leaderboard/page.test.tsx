// T-2 remediation · The /leaderboard primary-nav tab must source its DIRECTIONAL rows through the
// self-gating `getLeaderboard()` reader (mock ON → fixture; mock OFF → real fetch, honest-empty on
// absence/error) — NEVER the LEADERBOARD_ROWS fixture as a static default. A judge on /leaderboard
// with the demo flag OFF must see honest-empty, not fabricated rankings.
//
// getLeaderboard is mocked to isolate the PAGE's wiring from the network: the off-mock branch is
// represented by [] (or a rejection), the mock-ON branch by the fixture rows.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const getLeaderboardMock = vi.fn();
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getLeaderboard: () => getLeaderboardMock() };
});

import LeaderboardPage from './page';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';

afterEach(() => {
  vi.clearAllMocks();
});

describe('LeaderboardPage — directional rows sourced via getLeaderboard (T-2, honest off-mock)', () => {
  it('mock OFF: getLeaderboard returns [] → honest-empty, NEVER the LEADERBOARD_ROWS fixture', async () => {
    getLeaderboardMock.mockResolvedValue([]);
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getByTestId('lb-empty')).toBeInTheDocument());
    expect(screen.queryByTestId('lb-row')).toBeNull();
    // the fixture's signature agent must NOT be rendered off-mock
    expect(screen.queryByText(/Momentum FR/)).toBeNull();
  });

  it('mock OFF error: a fetch failure yields honest-empty, NEVER the fixture (T-2 fixture prohibition)', async () => {
    getLeaderboardMock.mockRejectedValue(new Error('backend unavailable'));
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getByTestId('lb-empty')).toBeInTheDocument());
    expect(screen.queryByTestId('lb-row')).toBeNull();
    expect(screen.queryByText(/Momentum FR/)).toBeNull();
  });

  it('mock ON path: renders exactly the rows getLeaderboard returns (fixture passed through, not defaulted)', async () => {
    getLeaderboardMock.mockResolvedValue(LEADERBOARD_ROWS);
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(LEADERBOARD_ROWS.length));
    expect(screen.queryByTestId('lb-empty')).toBeNull();
  });
});
