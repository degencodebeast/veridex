import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

// vi.hoisted so the mock fns exist before the hoisted vi.mock factories run (vitest 2.x hoisting rule).
const { getLeaderboard, isMockEnabled } = vi.hoisted(() => ({
  getLeaderboard: vi.fn(async () => [] as never[]),
  isMockEnabled: vi.fn(),
}));
vi.mock('@/lib/api', async (orig) => ({ ...(await orig<typeof import('@/lib/api')>()), getLeaderboard }));
vi.mock('@/lib/mock', async (orig) => ({ ...(await orig<typeof import('@/lib/mock')>()), isMockEnabled: () => isMockEnabled() }));

import LeaderboardPage from '@/app/(app)/leaderboard/page';
import MarketsPage from '@/app/(app)/markets/page';

beforeEach(() => { getLeaderboard.mockClear(); });
afterEach(() => { vi.clearAllMocks(); });

describe('leaderboard quarantine — no off-mock /leaderboard from either caller', () => {
  it('Leaderboard page does NOT call getLeaderboard off-mock', async () => {
    isMockEnabled.mockReturnValue(false);
    render(<LeaderboardPage />);
    await waitFor(() => {});
    expect(getLeaderboard).not.toHaveBeenCalled();
  });

  it('Markets page does NOT call getLeaderboard off-mock', async () => {
    isMockEnabled.mockReturnValue(false);
    render(<MarketsPage />);
    await waitFor(() => {});
    expect(getLeaderboard).not.toHaveBeenCalled();
  });

  it('mock mode is UNCHANGED — Leaderboard page still calls getLeaderboard under mock', async () => {
    isMockEnabled.mockReturnValue(true);
    render(<LeaderboardPage />);
    await waitFor(() => expect(getLeaderboard).toHaveBeenCalled());
  });
});
