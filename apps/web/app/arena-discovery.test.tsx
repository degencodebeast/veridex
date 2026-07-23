import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { FeedHealthState } from '@/lib/contracts';

// vi.hoisted so the mock fns exist before the hoisted vi.mock factory runs (vitest 2.x hoisting rule).
const { getCompetitions, getCockpitState, getFeedHealth } = vi.hoisted(() => ({
  getCompetitions: vi.fn(),
  getCockpitState: vi.fn(),
  getFeedHealth: vi.fn(),
}));
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getCompetitions,
  getCockpitState,
  getFeedHealth,
}));
vi.mock('@/lib/mock', async (orig) => ({ ...(await orig<typeof import('@/lib/mock')>()), isMockEnabled: () => false }));
vi.mock('next/navigation', () => ({ usePathname: () => '/arena' }));

const SAMPLE_FEED_HEALTH: FeedHealthState = {
  source_mode: 'replay', ws_live: false, connected: true, txline_configured: false,
  events_per_min: null, ticks_seen: 40, staleness_s: null, stale: true, fixture_id: null,
  anchor_status: 'not_applicable', last_tick_ts: null,
};
// Echo the `initial` snapshot the page fetched — exercises the page's discovery/selection/wiring
// without a real WebSocket. The cockpit's own streaming is covered by CockpitScreen.test.tsx.
vi.mock('@/hooks/useArenaStream', () => ({
  useArenaStream: (_id: string, initial: unknown, initialFeed?: FeedHealthState) => ({
    state: initial, wsStatus: 'connected', feedHealth: initialFeed ?? SAMPLE_FEED_HEALTH,
  }),
}));

import ArenaPage from '@/app/(app)/arena/page';

beforeEach(() => {
  getCockpitState.mockResolvedValue(sampleCockpitState);
  getFeedHealth.mockResolvedValue(SAMPLE_FEED_HEALTH);
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});
afterEach(() => { vi.clearAllMocks(); });

describe('Arena landing — fixture-selectable cockpit', () => {
  it('discovers running+finalized competitions into a FIXTURE selector (excludes draft), defaults to the first, and renders its cockpit inline', async () => {
    getCompetitions.mockResolvedValue([
      { competitionId: 'c_run00001', status: 'running', title: 'OfficialReplayLeague', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: null },
      { competitionId: 'c_fin00002', status: 'finalized', title: 'OfficialReplayLeague', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: 'r_1' },
      { competitionId: 'c_draft003', status: 'draft', title: 'Draft comp', sourceMode: null, executionMode: null, rosterSize: null, runId: null },
    ]);
    render(<ArenaPage />);

    // The FIXTURE selector replaces the old bare link list.
    const select = (await screen.findByLabelText('FIXTURE')) as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    // No bare <a> link list any more.
    expect(screen.queryByRole('link', { name: /OfficialReplayLeague/ })).toBeNull();

    // Exactly the two ACTIVE competitions become options (draft is excluded); labels disambiguate the
    // shared title with the short id + status.
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(2);
    expect(options[0].textContent).toContain('c_run000');
    expect(options[1].textContent).toContain('c_fin000');
    expect(screen.queryByRole('option', { name: /Draft comp/ })).toBeNull();

    // Defaults to the first discovered competition and loads ITS cockpit.
    expect(select.value).toBe('c_run00001');
    await waitFor(() => expect(getCockpitState).toHaveBeenCalledWith('c_run00001'));
    // The real cockpit renders inline (its CLV leaderboard landmark).
    expect(await screen.findByLabelText('CLV leaderboard')).toBeInTheDocument();
  });

  it('switches the cockpit when a different fixture is selected', async () => {
    getCompetitions.mockResolvedValue([
      { competitionId: 'c_run00001', status: 'running', title: 'OfficialReplayLeague', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: null },
      { competitionId: 'c_fin00002', status: 'finalized', title: 'OfficialReplayLeague', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: 'r_1' },
    ]);
    render(<ArenaPage />);
    const select = (await screen.findByLabelText('FIXTURE')) as HTMLSelectElement;
    await waitFor(() => expect(getCockpitState).toHaveBeenCalledWith('c_run00001'));

    fireEvent.change(select, { target: { value: 'c_fin00002' } });
    await waitFor(() => expect(getCockpitState).toHaveBeenCalledWith('c_fin00002'));
  });

  it('renders the honest empty state when nothing is running/finalized', async () => {
    getCompetitions.mockResolvedValue([]);
    render(<ArenaPage />);
    await waitFor(() => expect(screen.getByText(/No live competition/i)).toBeTruthy());
    expect(getCockpitState).not.toHaveBeenCalled();
  });

  it('is honest-empty (never a fabricated cockpit) when discovery fails', async () => {
    getCompetitions.mockRejectedValue(new Error('backend down'));
    render(<ArenaPage />);
    await waitFor(() => expect(screen.getByText(/No live competition/i)).toBeTruthy());
    expect(getCockpitState).not.toHaveBeenCalled();
  });

  it('renders the FIXTURE selector but NO fabricated cockpit when the selected competition fails to load', async () => {
    // Discovery succeeds (the competition is real + selectable) but its cockpit snapshot fails — the
    // honest outcome is: keep the selector, render no cockpit; NEVER a fabricated/placeholder cockpit.
    getCompetitions.mockResolvedValue([
      { competitionId: 'c_fin00002', status: 'finalized', title: 'OfficialReplayLeague', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: 'r_1' },
    ]);
    getCockpitState.mockRejectedValue(new Error('cockpit backend down'));
    render(<ArenaPage />);
    const select = (await screen.findByLabelText('FIXTURE')) as HTMLSelectElement;
    expect(select.value).toBe('c_fin00002');
    await waitFor(() => expect(getCockpitState).toHaveBeenCalledWith('c_fin00002'));
    // No fabricated cockpit — the leaderboard landmark is absent.
    expect(screen.queryByLabelText('CLV leaderboard')).toBeNull();
  });
});
