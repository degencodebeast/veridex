import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LeaderboardScreen } from '@/components/screens/LeaderboardScreen';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
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

  it('renders a mixed-source row honestly as "mixed" — not collapsed to replay', () => {
    render(<LeaderboardScreen rows={[mk({ agent_id: 'm', agent_name: 'Mixed Co', source_mode: 'mixed' })]} />);
    const src = within(screen.getByTestId('lb-row')).getByTestId('lb-source');
    expect(src).toHaveTextContent(/mixed/i);
    expect(src).not.toHaveTextContent(/replay/i);
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

describe('LeaderboardScreen — Maker Arena lane (MM-R1)', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
  });

  it('defaults to the Directional lane — the existing board is untouched', () => {
    render(<LeaderboardScreen />);
    expect(screen.getByRole('radio', { name: 'Directional' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getAllByTestId('lb-row').length).toBe(LEADERBOARD_ROWS.length);
    expect(screen.queryByTestId('lb-maker-row')).toBeNull();
  });

  it('the lane toggle switches to the maker board and is URL-addressable (?lane=maker)', async () => {
    const user = userEvent.setup();
    render(<LeaderboardScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    expect(screen.queryByTestId('lb-row')).toBeNull();
    expect(screen.getAllByTestId('lb-maker-row').length).toBe(2);
    expect(new URLSearchParams(window.location.search).get('lane')).toBe('maker');
  });

  it('maker board ranks by avg_toxicity_loss_bps ASC (lower = better) — never CLV (SEC-005)', async () => {
    const user = userEvent.setup();
    render(<LeaderboardScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const rows = screen.getAllByTestId('lb-maker-row');
    expect(within(rows[0]).getByTestId('lb-maker-agent')).toHaveTextContent('txline-fair-mm');
    expect(within(rows[1]).getByTestId('lb-maker-agent')).toHaveTextContent('naive-mm');
    const toxicities = rows.map((r) => parseFloat(within(r).getByTestId('lb-maker-toxicity').textContent!));
    expect([...toxicities]).toEqual([...toxicities].sort((a, b) => a - b));
    // the maker table itself carries no AVG CLV column (the lane-note prose mentioning
    // "Avg CLV" descriptively is expected and stays visible in both lanes).
    expect(within(screen.getByRole('table')).queryByText(/avg clv/i)).toBeNull();
  });

  it('EXEC EDGE renders the literal null and the n=18 small-sample caveat is always shown', async () => {
    const user = userEvent.setup();
    render(<LeaderboardScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    screen.getAllByTestId('lb-maker-edge').forEach((cell) => expect(cell).toHaveTextContent('null'));
    expect(screen.getAllByText(/n=18/i).length).toBeGreaterThan(0);
  });

  it('shows the SEPARATED falsification headline with the Δ and CI (leads with the claim, not the mean)', async () => {
    const user = userEvent.setup();
    render(<LeaderboardScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    expect(screen.getAllByText(/separated/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/\+43/)).toBeInTheDocument();
    expect(screen.getByText('[34, 52]')).toBeInTheDocument();
  });

  it('markout is a muted diagnostic column, never the rank axis — matches the sealed fixture (SEC-005)', () => {
    expect(MAKER_ARENA_RESULT.rank_axis).toBe('avg_toxicity_loss_bps');
    expect(MAKER_ARENA_RESULT.leaderboard.every((r) => r.real_executable_edge_bps === null)).toBe(true);
  });

  it('SEC-005: maker rows never carry a directional rank/CLV key', () => {
    for (const row of MAKER_ARENA_RESULT.leaderboard) {
      expect(Object.keys(row)).not.toContain('avg_clv_bps');
      expect(Object.keys(row)).not.toContain('rank');
      expect(Object.keys(row)).toContain('maker_rank');
    }
  });
});
