import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { FeedHealthState } from '@/lib/contracts';

// vi.hoisted so the mock fns exist before the hoisted vi.mock factory runs (vitest 2.x hoisting rule
// — a bare top-level const would `Cannot access 'getCompetitions' before initialization` in isolation).
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
// Echo the fetched `initial` snapshot — exercises page wiring without a real WebSocket (the cockpit's
// streaming has its own coverage in CockpitScreen.test.tsx).
vi.mock('@/hooks/useArenaStream', () => ({
  useArenaStream: (_id: string, initial: unknown, initialFeed?: FeedHealthState) => ({
    state: initial, wsStatus: 'connected', feedHealth: initialFeed ?? SAMPLE_FEED_HEALTH,
  }),
}));

import CompetitionsPage from '@/app/(app)/competitions/page';
import ArenaPage from '@/app/(app)/arena/page';

// The record shape a REAL create+start produces (status='finalized' + run_id) — mirrors
// tests/test_competition_discovery.py, NOT a fabricated `running`.
const FINALIZED = {
  competitionId: 'c_fin', status: 'finalized', title: 'NLD v MAR', sourceMode: 'replay',
  executionMode: 'paper', rosterSize: 2, runId: 'r_1',
};

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

describe('cross-page acceptance — a finalized competition is discoverable on Competitions AND Arena, both opening the cockpit', () => {
  it('Competitions page lists the real finalized record linking /arena/{id}, with a coherent count and no contradictions', async () => {
    getCompetitions.mockResolvedValue([FINALIZED]);
    render(<CompetitionsPage />);
    const link = await screen.findByRole('link', { name: /NLD v MAR/ });
    expect(link.getAttribute('href')).toBe('/arena/c_fin');
    // coherent count reflects the real record; the contradictory mock surfaces are absent off-mock.
    expect(screen.getByTestId('real-total').textContent).toContain('1');
    expect(screen.queryByTestId('stat-total')).toBeNull();
    expect(screen.queryByTestId('all-competitions-empty')).toBeNull();
  });

  it('Arena page discovers the same finalized record as a FIXTURE option and opens its /arena/{id} cockpit inline', async () => {
    getCompetitions.mockResolvedValue([FINALIZED]);
    render(<ArenaPage />);
    // Discoverable as the (default-selected) FIXTURE option — its title + id, not a bare link.
    const select = (await screen.findByLabelText('FIXTURE')) as HTMLSelectElement;
    const option = screen.getByRole('option', { name: /NLD v MAR/ }) as HTMLOptionElement;
    expect(option.value).toBe('c_fin');
    expect(select.value).toBe('c_fin');
    // Opening the same competition's cockpit inline (getCockpitState scoped to c_fin) — the Arena no
    // longer duplicates it as a bare link.
    await waitFor(() => expect(getCockpitState).toHaveBeenCalledWith('c_fin'));
    expect(screen.queryByRole('link', { name: /NLD v MAR/ })).toBeNull();
  });
});
