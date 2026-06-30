import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MatchStatePanel } from '@/components/screens/cockpit/MatchStatePanel';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';

describe('MatchStatePanel (REQ-040 / AC-012)', () => {
  it('renders score, phase, minute, goals, cards, corners', () => {
    render(<MatchStatePanel match={sampleCockpitState.match} />);
    expect(screen.getByText(/1\s*[-–]\s*1/)).toBeInTheDocument(); // score
    expect(screen.getByText(/H2/)).toBeInTheDocument();           // phase
    expect(screen.getByText(/62'/)).toBeInTheDocument();          // minute
    // exact-string match: the "corners" stat label only (the footnote also contains
    // the word "corners", so a substring /corners/i would match two elements).
    expect(screen.getByText('corners')).toBeInTheDocument();
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
