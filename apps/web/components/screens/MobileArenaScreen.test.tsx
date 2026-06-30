import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MobileArenaScreen } from '@/components/screens/MobileArenaScreen';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';

describe('MobileArenaScreen (REQ-027)', () => {
  it('constrains to a 392px phone frame', () => {
    render(<MobileArenaScreen />);
    expect(screen.getByTestId('phone-frame')).toHaveAttribute('data-width', '392');
  });

  it('renders the leaderboard as stacked cards ranked by Avg CLV (CLV-only)', () => {
    render(<MobileArenaScreen />);
    const cards = screen.getAllByTestId('mobile-lb-card');
    expect(cards.length).toBe(LEADERBOARD_ROWS.length);
    expect(within(cards[0]).getByTestId('mobile-rank')).toHaveTextContent('1');
    expect(within(cards[0]).getByText(/Momentum FR/)).toBeInTheDocument(); // highest avg_clv
  });

  it('renders a fixed bottom tab bar with 4 destinations', () => {
    render(<MobileArenaScreen />);
    const bar = screen.getByTestId('bottom-tabs');
    for (const t of ['Arena', 'Agents', 'Proof', 'Rank']) {
      expect(within(bar).getByRole('link', { name: t })).toBeInTheDocument();
    }
  });

  it('does NOT dress the static demo header as a live feed (honesty)', () => {
    render(<MobileArenaScreen />);
    // no pulsing SCORING pill over hardcoded constants
    expect(screen.queryByText(/SCORING/)).toBeNull();
    const match = screen.getByTestId('mobile-match');
    expect(within(match).queryByText(/^Live$/)).toBeNull(); // no live badge over the static score
    expect(within(match).getByText(/mock/i)).toBeInTheDocument(); // honestly labelled demo/mock data
  });
});
