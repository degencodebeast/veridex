import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { CompetitionsScreen } from '@/components/screens/CompetitionsScreen';

describe('CompetitionsScreen (REQ-014)', () => {
  it('routes a live competition to Enter Arena', () => {
    render(<CompetitionsScreen />);
    const live = screen.getByTestId('comp-wc-fra-bra');
    expect(within(live).getByRole('link', { name: /enter arena/i })).toHaveAttribute('href', '/arena/wc-fra-bra');
  });

  it('routes an upcoming competition to Join and a settled one to Proof', () => {
    render(<CompetitionsScreen />);
    expect(within(screen.getByTestId('comp-wc-arg-ger')).getByRole('link', { name: /join/i })).toBeInTheDocument();
    expect(within(screen.getByTestId('comp-wc-esp-ned')).getByRole('link', { name: /proof/i }))
      .toHaveAttribute('href', '/proof/run_esp_ned_01');
  });

  it('shows the EVENTS/MIN + WS LIVE liveness tiles', () => {
    render(<CompetitionsScreen />);
    expect(screen.getAllByText(/EVENTS\/MIN/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/WS LIVE/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders a Recent Settled strip linking to a proof', () => {
    render(<CompetitionsScreen />);
    const strip = screen.getByTestId('recent-settled');
    expect(within(strip).getByRole('link', { name: /ESP v NED/i })).toHaveAttribute('href', '/proof/run_esp_ned_01');
  });

  it('is honest when empty — renders no fabricated competition rows (REQ-014 honest states)', () => {
    render(<CompetitionsScreen comps={[]} />);
    expect(screen.queryAllByTestId(/^comp-/)).toHaveLength(0);
    // the recent-settled strip exists but carries no fake proof links
    const strip = screen.getByTestId('recent-settled');
    expect(within(strip).queryAllByRole('link')).toHaveLength(0);
  });
});
