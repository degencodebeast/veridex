// F-4 residual (T-2) · /competitions is the "Enter App" landing tab — the highest-visibility
// Potemkin surface. With the demo flag OFF a judge here must see honest-empty competitions +
// rewards, NEVER the COMPETITIONS / MY_REWARDS fixtures. The per-tab mock gate (isMockEnabled) is
// the ONLY thing that may surface those fixtures, and it must gate the DATA — not merely the
// LEADER-CLV demo cell. There is no GET-list adapter that can HONESTLY populate the rich
// CompetitionSummary view-model (the wire summary carries no title/proof_mode), so off-mock the
// page renders nothing rather than fabricate.
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import CompetitionsPage from './page';
import { COMPETITIONS } from '@/lib/fixtures/catalog';

afterEach(() => { vi.unstubAllEnvs(); });

describe('CompetitionsPage — data sourced via the mock gate (T-2, honest off-mock)', () => {
  it('mock OFF: renders honest-empty — NONE of the COMPETITIONS fixtures leak', async () => {
    render(<CompetitionsPage />);
    // the derived TOTAL count is the honest tell: 0, not the fixture count.
    await waitFor(() => expect(screen.getByTestId('stat-total')).toHaveTextContent('0'));
    // not a single fabricated competition row, and the signature fixture title is absent.
    expect(screen.queryAllByTestId(/^comp-/)).toHaveLength(0);
    expect(screen.queryByText(/World Cup · FRA v BRA/)).toBeNull();
    // honest-empty affordance is present for the all-competitions list.
    expect(screen.getByTestId('all-competitions-empty')).toBeInTheDocument();
  });

  it('mock ON: the labeled DEMO fixtures are surfaced (passed through, not fabricated)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<CompetitionsPage />);
    await waitFor(() =>
      expect(screen.getByTestId('stat-total')).toHaveTextContent(String(COMPETITIONS.length)),
    );
    expect(screen.getByTestId('comp-wc-fra-bra')).toBeInTheDocument();
    // the fixture title renders in both the LIVE card and the all-competitions row → >= 1 match.
    expect(screen.getAllByText(/World Cup · FRA v BRA/).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByTestId('all-competitions-empty')).toBeNull();
  });
});
