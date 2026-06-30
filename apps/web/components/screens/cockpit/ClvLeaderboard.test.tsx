import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ClvLeaderboard } from '@/components/screens/cockpit/ClvLeaderboard';
import type { LeaderboardRow } from '@/lib/contracts';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

// WD-7 confidence fields are part of the view-model row and must be supplied.
const base: Omit<LeaderboardRow, 'rank' | 'agent_id' | 'agent_name' | 'avg_clv_bps' | 'eligibility_badge'> = {
  agent_kind: 'LLM', runs: 5, total_clv_bps: 50, sim_pnl: 10, brier: 0.2, max_drawdown: -5,
  action_count: 20, valid_pct: 90, proof_mode: 'verified', anchor_status: 'pending', source_mode: 'live',
  valid_count: 20, clv_confidence: 'high', low_sample: false,
};

describe('ClvLeaderboard (REQ-011 / SEC-005)', () => {
  it('renders the REQ-011 column set with proxy marks', () => {
    render(<ClvLeaderboard rows={[{ ...base, rank: 1, agent_id: 'a', agent_name: 'A', avg_clv_bps: 20, eligibility_badge: 'eligible' }]} />);
    expect(screen.getByText('AVG CLV')).toBeInTheDocument();
    expect(screen.getByText(/SIM PNL/)).toHaveTextContent('ⓟ');
    expect(screen.getByText(/BRIER/)).toHaveTextContent('ⓟ');
  });

  it('orders by Avg CLV desc; a not-eligible higher-CLV agent still ranks above an eligible one (AC-005)', () => {
    const rows: LeaderboardRow[] = [
      { ...base, rank: 1, agent_id: 'hi', agent_name: 'HighCLV', avg_clv_bps: 25, eligibility_badge: 'not-eligible' },
      { ...base, rank: 2, agent_id: 'lo', agent_name: 'LowCLV', avg_clv_bps: 12, eligibility_badge: 'eligible' },
    ];
    render(<ClvLeaderboard rows={rows} />);
    const bodyRows = screen.getAllByRole('row').slice(1); // drop header
    expect(within(bodyRows[0]).getByText('HighCLV')).toBeInTheDocument();
    expect(within(bodyRows[1]).getByText('LowCLV')).toBeInTheDocument();
  });

  it('surfaces a low-confidence indicator for low_sample rows WITHOUT reordering (WD-7 / SEC-005)', () => {
    const rows: LeaderboardRow[] = [
      { ...base, rank: 1, agent_id: 'hi', agent_name: 'HighCLV', avg_clv_bps: 25, eligibility_badge: 'eligible', low_sample: true, clv_confidence: 'low', valid_count: 3 },
      { ...base, rank: 2, agent_id: 'lo', agent_name: 'LowCLV', avg_clv_bps: 12, eligibility_badge: 'eligible' },
    ];
    render(<ClvLeaderboard rows={rows} />);
    const bodyRows = screen.getAllByRole('row').slice(1);
    // Indicator present on the low-sample row...
    expect(within(bodyRows[0]).getByText(/low.?confidence/i)).toBeInTheDocument();
    // ...and the higher-CLV low-sample agent STILL ranks first (confidence is display-only).
    expect(within(bodyRows[0]).getByText('HighCLV')).toBeInTheDocument();
    expect(within(bodyRows[1]).getByText('LowCLV')).toBeInTheDocument();
  });

  it('REORDERS ascending input into Avg-CLV-desc (rank IS Avg CLV — would fail a no-op comparator)', () => {
    // Input is in ASCENDING avg_clv order; a no-op/identity sort would render
    // [12, 25] and fail. The metric-sort must actively reorder to [25, 12].
    const rows: LeaderboardRow[] = [
      { ...base, rank: 99, agent_id: 'lo', agent_name: 'LowCLV', avg_clv_bps: 12, eligibility_badge: 'eligible' },
      { ...base, rank: 1, agent_id: 'hi', agent_name: 'HighCLV', avg_clv_bps: 25, eligibility_badge: 'eligible' },
    ];
    render(<ClvLeaderboard rows={rows} />);
    const bodyRows = screen.getAllByRole('row').slice(1);
    expect(within(bodyRows[0]).getByText('HighCLV')).toBeInTheDocument(); // avg 25 first
    expect(within(bodyRows[1]).getByText('LowCLV')).toBeInTheDocument();  // avg 12 second
  });

  it('labels a not_applicable anchor as n/a, not "Not Anchored"', () => {
    const { container } = render(
      <ClvLeaderboard rows={[{ ...base, rank: 1, agent_id: 'a', agent_name: 'A', avg_clv_bps: 20, eligibility_badge: 'eligible', anchor_status: 'not_applicable' }]} />,
    );
    expect(screen.getByText('n/a')).toBeInTheDocument();
    expect(container.textContent?.toLowerCase()).not.toContain('not anchored');
  });

  it('shows the rank-rule + proxy disclaimers', () => {
    render(<ClvLeaderboard rows={[{ ...base, rank: 1, agent_id: 'a', agent_name: 'A', avg_clv_bps: 20, eligibility_badge: 'eligible' }]} />);
    expect(screen.getByText(/Rank is Avg CLV only/i)).toBeInTheDocument();
    expect(screen.getByText(/simulated proxies/i)).toBeInTheDocument();
  });
});
