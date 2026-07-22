import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// vi.hoisted so the mock fn exists before the hoisted vi.mock factory runs (vitest 2.x hoisting rule
// — a bare top-level const would `Cannot access 'getCompetitions' before initialization` in isolation).
const { getCompetitions } = vi.hoisted(() => ({ getCompetitions: vi.fn() }));
vi.mock('@/lib/api', async (orig) => ({ ...(await orig<typeof import('@/lib/api')>()), getCompetitions }));
vi.mock('@/lib/mock', async (orig) => ({ ...(await orig<typeof import('@/lib/mock')>()), isMockEnabled: () => false }));

import ArenaPage from '@/app/(app)/arena/page';

afterEach(() => { vi.clearAllMocks(); });

describe('Arena landing discovery', () => {
  it('surfaces BOTH running and finalized competitions (real enum) linking the /arena/{id} cockpit, and excludes draft', async () => {
    // Real lifecycle enum values only: `running` opens the live cockpit; `finalized` is the status a
    // create+start synchronously persists and must stay discoverable; `draft` is NOT surfaced.
    getCompetitions.mockResolvedValue([
      { competitionId: 'c_run', status: 'running', title: 'France v Morocco (live)', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: null },
      { competitionId: 'c_fin', status: 'finalized', title: 'Argentina v Croatia (final)', sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: 'r_1' },
      { competitionId: 'c_draft', status: 'draft', title: 'Draft comp', sourceMode: null, executionMode: null, rosterSize: null, runId: null },
    ]);
    render(<ArenaPage />);
    const runLink = await screen.findByRole('link', { name: /France v Morocco/ });
    expect(runLink.getAttribute('href')).toBe('/arena/c_run');
    // finalized is still discoverable — the exact competition a create+start produces must NOT vanish.
    expect(screen.getByRole('link', { name: /Argentina v Croatia/ }).getAttribute('href')).toBe('/arena/c_fin');
    // draft is excluded from the Arena landing.
    expect(screen.queryByRole('link', { name: /Draft comp/ })).toBeNull();
    // Maker Lab entry lives under Arena (spec §6.2).
    expect(screen.getByRole('link', { name: /Maker Lab/i })).toBeTruthy();
  });

  it('renders the honest empty state when nothing is running/finalized', async () => {
    getCompetitions.mockResolvedValue([]);
    render(<ArenaPage />);
    await waitFor(() => expect(screen.getByText(/No live competition/i)).toBeTruthy());
  });
});
