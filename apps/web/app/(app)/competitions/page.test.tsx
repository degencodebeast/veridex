// F-4 residual (T-2) · /competitions is the "Enter App" landing tab — the highest-visibility
// Potemkin surface. With the demo flag OFF a judge here must see honest-empty competitions,
// NEVER the COMPETITIONS / MY_REWARDS fixtures. Off-mock the page reads the REAL records (GET
// /competitions) and renders the coherent real-records honest empty state — with NO contradictory
// mock `TOTAL 0` band or "No competitions to show." row alongside it (finding 6.2). The per-tab
// mock gate (isMockEnabled) is the ONLY thing that surfaces the labeled DEMO fixtures.
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import CompetitionsPage from './page';
import { COMPETITIONS } from '@/lib/fixtures/catalog';

// vi.hoisted so the mock fn exists before the hoisted vi.mock factory runs (vitest 2.x hoisting rule
// — a bare top-level const would `Cannot access 'getCompetitions' before initialization` in isolation).
const { getCompetitions } = vi.hoisted(() => ({ getCompetitions: vi.fn() }));
vi.mock('@/lib/api', async (orig) => ({ ...(await orig<typeof import('@/lib/api')>()), getCompetitions }));

afterEach(() => { vi.unstubAllEnvs(); vi.clearAllMocks(); });

describe('CompetitionsPage — data sourced via the mock gate (T-2, honest off-mock)', () => {
  it('mock OFF: renders the real-records honest empty state — NONE of the COMPETITIONS fixtures leak, and NO contradictory TOTAL 0 / "No competitions to show."', async () => {
    getCompetitions.mockResolvedValue([]); // off-mock, no real records
    render(<CompetitionsPage />);
    // the honest real-records empty state (Create + Browse CTAs) is the off-mock tell.
    await waitFor(() => expect(screen.getByTestId('competitions-empty')).toBeInTheDocument());
    // not a single fabricated competition row, and the signature fixture title is absent.
    expect(screen.queryAllByTestId(/^comp-/)).toHaveLength(0);
    expect(screen.queryByText(/World Cup · FRA v BRA/)).toBeNull();
    // the CONTRADICTORY mock surfaces are gone off-mock (finding 6.2): no TOTAL 0 band, no
    // "No competitions to show." empty row alongside the real-records state.
    expect(screen.queryByTestId('stat-total')).toBeNull();
    expect(screen.queryByTestId('all-competitions-empty')).toBeNull();
  });

  it('mock ON: the labeled DEMO fixtures are surfaced (passed through, not fabricated)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<CompetitionsPage />);
    await waitFor(() =>
      expect(screen.getByTestId('stat-total')).toHaveTextContent(String(COMPETITIONS.length)),
    );
    expect(screen.getByTestId('comp-wc-fra-bra')).toBeInTheDocument();
    expect(screen.getAllByText(/World Cup · FRA v BRA/).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByTestId('all-competitions-empty')).toBeNull();
  });
});
