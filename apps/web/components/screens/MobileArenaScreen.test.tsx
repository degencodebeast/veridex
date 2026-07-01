import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MobileArenaScreen } from '@/components/screens/MobileArenaScreen';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import { GLOSSARY } from '@/lib/glossary';
import type { MatchState } from '@/lib/contracts';

vi.mock('next/navigation', () => ({ usePathname: () => '/m/arena' }));

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

// the live emptyMatch (scores not wired): no coverage
const EMPTY: MatchState = {
  fixture: '', phase: 'NS', minute: null, goals: [0, 0], yellow: [0, 0], red: [0, 0], corners: [0, 0], status: 'scheduled',
};

describe('MobileArenaScreen (Cockpit collapsed to one scroll column)', () => {
  it('constrains to a 392px phone frame with a fixed bottom tab bar (4 destinations)', () => {
    render(<MobileArenaScreen initial={sampleCockpitState} />);
    expect(screen.getByTestId('phone-frame')).toHaveAttribute('data-width', '392');
    const bar = screen.getByTestId('bottom-tabs');
    for (const t of ['Arena', 'Agents', 'Proof', 'Rank']) {
      expect(within(bar).getByRole('link', { name: t })).toBeInTheDocument();
    }
  });

  it('REUSES the closed Cockpit panels in ONE scroll column (RunHeader / MatchState / CLV / event stream)', () => {
    render(<MobileArenaScreen initial={sampleCockpitState} />);
    const col = screen.getByTestId('mobile-column');
    expect(within(col).getByText(/FRA v BRA/)).toBeInTheDocument();          // RunHeader
    expect(within(col).getByLabelText('Match state')).toBeInTheDocument();   // MatchStatePanel
    expect(within(col).getByLabelText('CLV leaderboard')).toBeInTheDocument(); // ClvLeaderboard
    expect(within(col).getByLabelText('Canonical event stream')).toBeInTheDocument();
  });

  it('inherits the MatchState B/C honesty: live-empty shows pending scores-feed, NO fabricated stats, NO clock', () => {
    render(<MobileArenaScreen initial={{ ...sampleCockpitState, match: EMPTY }} />);
    expect(screen.getByTestId('match-empty')).toHaveTextContent(/pending/i);
    expect(screen.queryByTestId('match-stats')).toBeNull();     // no fabricated 0–0 stats
    expect(screen.queryByText(/\d+'/)).toBeNull();              // no live minute/clock
  });

  it('InfoTip copy is single-sourced from lib/glossary.ts (inherited from the reused panels)', () => {
    render(<MobileArenaScreen initial={sampleCockpitState} />);
    expect(screen.getByText(GLOSSARY.clv.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.source_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.proof_mode.definition)).toBeInTheDocument();
  });
});
