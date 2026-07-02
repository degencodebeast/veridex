import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MatchStatePanel } from '@/components/screens/cockpit/MatchStatePanel';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { MatchState } from '@/lib/contracts';

// The live emptyMatch (getCockpitState → emptyMatch when scores aren't wired): no coverage.
const EMPTY: MatchState = {
  fixture: '', phase: 'NS', minute: null, goals: [0, 0], yellow: [0, 0], red: [0, 0], corners: [0, 0], status: 'scheduled',
};

describe('MatchStatePanel (REQ-040 / AC-012)', () => {
  it('renders score, phase, minute, goals, cards, corners', () => {
    render(<MatchStatePanel match={sampleCockpitState.match} />);
    expect(screen.getByText(/1\s*[-–]\s*1/)).toBeInTheDocument(); // score
    expect(screen.getByText(/H2/)).toBeInTheDocument();           // phase
    expect(screen.getByText(/62'/)).toBeInTheDocument();          // minute
    // exact-string match: the "corners" stat label only (the footnote also contains
    // the word "corners", so a substring /corners/i would match two elements).
    expect(screen.getByText('corners')).toBeInTheDocument();
    expect(screen.getByTestId('match-stats')).toBeInTheDocument(); // populated stat set
  });

  it('LIVE honest-empty (no scores feed): stats/phase are PENDING (B, wireable), NO fabricated zeros, NO clock (C)', () => {
    render(<MatchStatePanel match={EMPTY} />);
    // honest-empty framing — real upstream, pending the TxLINE scores-feed normalizer
    expect(screen.getByTestId('match-empty')).toHaveTextContent(/pending/i);
    expect(screen.getByTestId('match-empty')).toHaveTextContent(/scores[- ]?feed/i);
    // no fabricated 0–0 stat panel rendered as if real
    expect(screen.queryByTestId('match-stats')).toBeNull();
    // no live minute/clock implied (C — TxLINE has no elapsed clock; never a "connecting clock…" placeholder)
    expect(screen.queryByText(/\d+'/)).toBeNull();
    expect(screen.queryByText(/connecting|clock/i)).toBeNull();
  });

  it('labels cards & corners as stats, not markets', () => {
    render(<MatchStatePanel match={sampleCockpitState.match} />);
    expect(screen.getByText(/cards & corners are match stats, not tradable markets/i)).toBeInTheDocument();
  });

  it('NEVER renders possession (AC-012)', () => {
    const { container } = render(<MatchStatePanel match={sampleCockpitState.match} />);
    expect(container.textContent?.toLowerCase()).not.toContain('possession');
  });
});
