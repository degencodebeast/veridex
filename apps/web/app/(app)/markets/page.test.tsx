// T-2 remediation · The /markets primary-nav tab must NOT show fabricated markets with the demo flag
// OFF. Odds/fixtures have NO backend reader, so they surface ONLY under isMockEnabled() (demo); the
// feed-health + eligible-agents rails source through the self-gating getFeedHealth()/getLeaderboard()
// readers (mock ON → fixture; mock OFF → real fetch, honest-empty on absence/error). A judge on
// /markets with the flag OFF must see honest-empty, never fabricated markets/feed/rankings.
//
// The two readers are mocked to isolate the PAGE's wiring from the network; the mock flag is driven
// by NEXT_PUBLIC_VERIDEX_MOCK (isMockEnabled reads it) so odds/fixtures gate exactly as in the app.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';

const getFeedHealthMock = vi.fn();
const getLeaderboardMock = vi.fn();
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getFeedHealth: () => getFeedHealthMock(), getLeaderboard: () => getLeaderboardMock() };
});

import MarketsPage from './page';
import { FEED_HEALTH, LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';

afterEach(() => { vi.clearAllMocks(); vi.unstubAllEnvs(); });

describe('MarketsPage — honest off-mock (T-2): odds/fixtures gated by the mock flag', () => {
  it('mock OFF: no fixtures/odds render — honest-empty prompt, NEVER the ODDS_UPDATES/FIXTURES fixture', async () => {
    getFeedHealthMock.mockResolvedValue(null);
    getLeaderboardMock.mockResolvedValue([]);
    render(<MarketsPage />);
    // the readers still fire (self-gating) — wait for the wiring to settle
    await waitFor(() => expect(getLeaderboardMock).toHaveBeenCalled());
    expect(screen.getByText(/select a fixture/i)).toBeInTheDocument();
    // the demo fixture must NOT leak off-mock: no fixture button, no decoded odds, no team names.
    expect(screen.queryByTestId('fixture-18172280')).toBeNull();
    expect(screen.queryByTestId('families')).toBeNull();
    expect(screen.queryByText('1.472')).toBeNull();
    expect(screen.queryByText(/FRA/)).toBeNull();
  });

  it('mock OFF error: reader failures yield honest-empty rails, still NEVER the fixture', async () => {
    getFeedHealthMock.mockRejectedValue(new Error('backend unavailable'));
    getLeaderboardMock.mockRejectedValue(new Error('backend unavailable'));
    render(<MarketsPage />);
    await waitFor(() => expect(getLeaderboardMock).toHaveBeenCalled());
    expect(screen.getByText(/select a fixture/i)).toBeInTheDocument();
    expect(screen.queryByTestId('fixture-18172280')).toBeNull();
    expect(screen.queryByText('1.472')).toBeNull();
  });

  it('mock ON: the demo fixtures surface — fixtures list + odds tables populate (labeled demo by the MockBanner)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    getFeedHealthMock.mockResolvedValue(FEED_HEALTH);
    getLeaderboardMock.mockResolvedValue(LEADERBOARD_ROWS);
    render(<MarketsPage />);
    // odds/fixtures come from the mock gate (synchronous with the flag) → first fixture auto-selects.
    await waitFor(() => expect(screen.getByTestId('fixture-18172280')).toBeInTheDocument());
    const fam = await screen.findByTestId('families');
    expect(within(fam).getAllByText('1.472').length).toBeGreaterThanOrEqual(1); // decoded decimal odds
  });
});
