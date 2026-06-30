import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LeaderboardScreen } from '@/components/screens/LeaderboardScreen';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import type { LeaderboardRow } from '@/lib/catalog';

function mk(p: Partial<LeaderboardRow>): LeaderboardRow {
  return {
    rank: 0, agent_id: 'a', agent_name: 'A', agent_kind: 'numeric', runs: 1,
    avg_clv_bps: 0, total_clv_bps: 0, sim_pnl: 0, brier: 0, max_drawdown: 0,
    action_count: 0, valid_count: 50, valid_pct: 90, proof_mode: 'reproducible',
    eligibility_badge: 'eligible', anchor_status: 'anchored', source_mode: 'live',
    clv_confidence: 'high', low_sample: false, ...p,
  };
}

describe('LeaderboardScreen (REQ-013 / AC-005 / WD-7)', () => {
  it('ranks by Avg CLV only — the not-eligible top-CLV agent is rank #1 (AC-005)', () => {
    render(<LeaderboardScreen />);
    const rows = screen.getAllByTestId('lb-row');
    const first = within(rows[0]);
    expect(first.getByTestId('lb-rank')).toHaveTextContent('1');
    expect(first.getByTestId('lb-agent')).toHaveTextContent(/Momentum FR/);
    expect(within(rows[0]).getByText(/not eligible/i)).toBeInTheDocument();
  });

  it('rank is Avg-CLV-only — eligibility AND confidence are displayed but NEVER reorder (#7 teeth)', () => {
    // Shuffled/ascending input with adversarial trust signals on the TOP-CLV agent:
    // it is BOTH not-eligible (partial proof) AND low-confidence (tiny sample).
    const rows: LeaderboardRow[] = [
      mk({ agent_id: 'lowclv', agent_name: 'Low CLV Eligible', avg_clv_bps: 4, proof_mode: 'reproducible', valid_count: 200, clv_confidence: 'high', low_sample: false }),
      mk({ agent_id: 'topclv', agent_name: 'Top CLV Unproven', avg_clv_bps: 30, proof_mode: 'partial', valid_count: 3, clv_confidence: 'low', low_sample: true }),
      mk({ agent_id: 'midclv', agent_name: 'Mid CLV Eligible', avg_clv_bps: 12, proof_mode: 'verified', valid_count: 80, clv_confidence: 'high', low_sample: false }),
    ];
    render(<LeaderboardScreen rows={rows} />);
    const order = screen.getAllByTestId('lb-row').map((r) => within(r).getByTestId('lb-agent').textContent);
    expect(order[0]).toMatch(/Top CLV Unproven/);
    expect(order[1]).toMatch(/Mid CLV Eligible/);
    expect(order[2]).toMatch(/Low CLV Eligible/);
    // The #1 agent is simultaneously NOT-ELIGIBLE and LOW-SAMPLE — both surfaced, neither demotes it.
    const top = within(screen.getAllByTestId('lb-row')[0]);
    expect(top.getByText(/not eligible/i)).toBeInTheDocument();
    expect(top.getByText(/low sample/i)).toBeInTheDocument();
  });

  it('shows the rank-rule banner', () => {
    render(<LeaderboardScreen />);
    expect(screen.getByText(/Rank is Avg CLV only/i)).toBeInTheDocument();
  });

  it('marks Sim PnL and Brier as proxies with ⓟ', () => {
    render(<LeaderboardScreen />);
    expect(screen.getByText(/^SIM PNL/).textContent).toMatch(/ⓟ/);
    expect(screen.getByText(/^BRIER/).textContent).toMatch(/ⓟ/);
  });

  it('flags low-sample CLV but never hides the row (WD-7)', () => {
    render(<LeaderboardScreen />);
    const rows = screen.getAllByTestId('lb-row');
    expect(rows.length).toBe(LEADERBOARD_ROWS.length); // nothing hidden
    expect(screen.getAllByText(/low sample/i).length).toBeGreaterThanOrEqual(1);
  });

  it('filters by source without changing the CLV-only sort rule', async () => {
    const user = userEvent.setup();
    render(<LeaderboardScreen />);
    await user.click(screen.getByRole('radio', { name: 'REPLAY' }));
    const rows = screen.getAllByTestId('lb-row');
    rows.forEach((r) => expect(within(r).getByTestId('lb-source')).toHaveTextContent(/replay/i));
    // still ranked by avg clv desc within the filtered set (parseFloat tolerates the " bps" unit)
    const clvs = rows.map((r) => parseFloat(within(r).getByTestId('lb-clv').textContent!.replace('+', '')));
    expect([...clvs]).toEqual([...clvs].sort((a, b) => b - a));
  });
});
