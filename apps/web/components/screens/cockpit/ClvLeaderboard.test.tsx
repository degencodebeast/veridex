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

  // F-5 honesty: competition rank is BACKEND-AUTHORITATIVE (CON-203). The server ranks + orders the
  // rows; ClvLeaderboard renders that order VERBATIM and must NEVER re-sort by a local CLV comparator
  // (a client re-sort would silently disagree with the sealed leaderboard the demo is proving).
  it('renders rows in backend order verbatim — does NOT re-sort by local Avg CLV', () => {
    // Backend order deliberately DISAGREES with an Avg-CLV-desc sort: the rank-1 row has the LOWER
    // avg. A local re-sort would flip these to [HighCLV, LowCLV] and fail; verbatim order must hold.
    const rows: LeaderboardRow[] = [
      { ...base, rank: 1, agent_id: 'lo', agent_name: 'BackendFirst', avg_clv_bps: 12, eligibility_badge: 'eligible' },
      { ...base, rank: 2, agent_id: 'hi', agent_name: 'BackendSecond', avg_clv_bps: 25, eligibility_badge: 'eligible' },
    ];
    render(<ClvLeaderboard rows={rows} />);
    const bodyRows = screen.getAllByRole('row').slice(1);
    expect(within(bodyRows[0]).getByText('BackendFirst')).toBeInTheDocument();  // rank 1, avg 12 — verbatim
    expect(within(bodyRows[1]).getByText('BackendSecond')).toBeInTheDocument(); // rank 2, avg 25 — verbatim
  });

  it('renders the backend rank in the # column (not a local 1-based position index)', () => {
    // A single row carrying a non-1 backend rank: a local `i + 1` index would render "1" and fail.
    render(<ClvLeaderboard rows={[{ ...base, rank: 7, agent_id: 'a', agent_name: 'A', avg_clv_bps: 20, eligibility_badge: 'eligible' }]} />);
    const bodyRow = screen.getAllByRole('row').slice(1)[0];
    expect(within(bodyRow).getByText('7')).toBeInTheDocument();
  });

  it('renders competition-absent metrics as an em-dash, never a fabricated 0 (honest gap)', () => {
    // The competition-scoped wire row carries NO sim_pnl/brier/max_drawdown/action_count/valid_pct;
    // the adapter maps them to null and the cell must render "—", never "0.0"/"0.000"/"0%".
    const row: LeaderboardRow = {
      ...base, rank: 1, agent_id: 'a', agent_name: 'A', avg_clv_bps: 20, eligibility_badge: 'eligible',
      sim_pnl: null, brier: null, max_drawdown: null, action_count: null, valid_pct: null,
    };
    render(<ClvLeaderboard rows={[row]} />);
    const bodyRow = screen.getAllByRole('row').slice(1)[0];
    expect(within(bodyRow).queryByText('0.0')).not.toBeInTheDocument();
    expect(within(bodyRow).queryByText('0.000')).not.toBeInTheDocument();
    expect(within(bodyRow).getAllByText('—').length).toBeGreaterThanOrEqual(5);
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
