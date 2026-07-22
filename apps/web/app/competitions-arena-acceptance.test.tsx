import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// vi.hoisted so the mock fn exists before the hoisted vi.mock factory runs (vitest 2.x hoisting rule
// — a bare top-level const would `Cannot access 'getCompetitions' before initialization` in isolation).
const { getCompetitions } = vi.hoisted(() => ({ getCompetitions: vi.fn() }));
vi.mock('@/lib/api', async (orig) => ({ ...(await orig<typeof import('@/lib/api')>()), getCompetitions }));
vi.mock('@/lib/mock', async (orig) => ({ ...(await orig<typeof import('@/lib/mock')>()), isMockEnabled: () => false }));

import CompetitionsPage from '@/app/(app)/competitions/page';
import ArenaPage from '@/app/(app)/arena/page';

// The record shape a REAL create+start produces (status='finalized' + run_id) — mirrors
// tests/test_competition_discovery.py, NOT a fabricated `running`.
const FINALIZED = {
  competitionId: 'c_fin', status: 'finalized', title: 'NLD v MAR', sourceMode: 'replay',
  executionMode: 'paper', rosterSize: 2, runId: 'r_1',
};

afterEach(() => { vi.clearAllMocks(); });

describe('cross-page acceptance — a finalized competition is discoverable on Competitions AND Arena, both linking the cockpit', () => {
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

  it('Arena page discovers the same finalized record linking the /arena/{id} cockpit', async () => {
    getCompetitions.mockResolvedValue([FINALIZED]);
    render(<ArenaPage />);
    const link = await screen.findByRole('link', { name: /NLD v MAR/ });
    expect(link.getAttribute('href')).toBe('/arena/c_fin');
  });
});
