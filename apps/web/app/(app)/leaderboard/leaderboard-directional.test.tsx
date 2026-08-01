// E3 · The /leaderboard directional board is wired to the REAL public board off-mock
// (GET /leaderboard/directional) through getDirectionalLeaderboard(): it renders honest DISPLAY NAMES
// (never opaque public ids), survives the REPLAY filter (all-replay provenance, M8), and honest-empties
// on a fetch error (NEVER a wire fixture). Mock ON is UNCHANGED (still the getLeaderboard() fixture path).
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const getDirectionalLeaderboardMock = vi.fn();
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getDirectionalLeaderboard: () => getDirectionalLeaderboardMock() };
});

import LeaderboardPage from './page';
import { adaptDirectionalLeaderboard } from '@/lib/api';
import type { DirectionalRow } from '@/lib/catalog';

// A raw /leaderboard/directional wire envelope: row 0 carries the backend's HONEST cross-run
// aggregate proof_mode "mixed" (runs disagree — veridex/leaderboard.py), row 1 an EARNED "verified".
const WIRE_MIXED = {
  board_kind: 'public_agents',
  rows: [
    {
      rank: 1, agent_id: 'pa_mix', display_name: 'Mixed Agent', public_agent_id: 'pa_mix',
      runs: 5, avg_clv_bps: 20, total_clv_bps: 100, sim_pnl: 1, brier: 0.2, max_drawdown: -2,
      action_count: 40, valid_pct: 90, proof_mode: 'mixed', eligibility_badge: 'not-eligible',
      anchor_status: 'pending', source_mode: 'replay', valid_count: 40, clv_confidence: 'high', low_sample: false,
    },
    {
      rank: 2, agent_id: 'pa_ver', display_name: 'Verified Agent', public_agent_id: 'pa_ver',
      runs: 3, avg_clv_bps: 12, total_clv_bps: 36, sim_pnl: 0.5, brier: 0.22, max_drawdown: -1,
      action_count: 20, valid_pct: 80, proof_mode: 'verified', eligibility_badge: 'eligible',
      anchor_status: 'anchored', source_mode: 'replay', valid_count: 20, clv_confidence: 'medium', low_sample: false,
    },
  ],
};

const ROWS: DirectionalRow[] = [
  {
    rank: 1, agent_id: 'pa_1', public_agent_id: 'pa_1', display_name: 'Falcon Value', agent_name: 'Falcon Value',
    agent_kind: '', runs: 5, avg_clv_bps: 20, total_clv_bps: 100, sim_pnl: 1, brier: 0.2, max_drawdown: -2,
    action_count: 40, valid_pct: 90, proof_mode: 'reproducible', proof_state: 'reproducible', eligibility_badge: 'eligible',
    anchor_status: 'anchored', source_mode: 'replay', valid_count: 40, clv_confidence: 'high', low_sample: false,
  },
  {
    rank: 2, agent_id: 'pa_2', public_agent_id: 'pa_2', display_name: 'Otter Momentum', agent_name: 'Otter Momentum',
    agent_kind: '', runs: 3, avg_clv_bps: 12, total_clv_bps: 36, sim_pnl: 0.5, brier: 0.22, max_drawdown: -1,
    action_count: 20, valid_pct: 80, proof_mode: 'partial', proof_state: 'partial', eligibility_badge: 'not-eligible',
    anchor_status: 'pending', source_mode: 'replay', valid_count: 20, clv_confidence: 'medium', low_sample: false,
  },
];

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/');
});

describe('LeaderboardPage — off-mock DIRECTIONAL board (E3)', () => {
  it('off-mock: renders REAL display names from /leaderboard/directional (never opaque ids)', async () => {
    getDirectionalLeaderboardMock.mockResolvedValue(ROWS);
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(2));
    expect(screen.getByText(/Falcon Value/)).toBeInTheDocument();
    expect(screen.getByText(/Otter Momentum/)).toBeInTheDocument();
    // the opaque public id is NOT the visible label
    expect(screen.queryByText('pa_1')).toBeNull();
    expect(getDirectionalLeaderboardMock).toHaveBeenCalledTimes(1);
  });

  it('survives the REPLAY filter (all-replay provenance, M8)', async () => {
    getDirectionalLeaderboardMock.mockResolvedValue(ROWS);
    const user = userEvent.setup();
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(2));
    await user.click(screen.getByRole('radio', { name: 'REPLAY' }));
    expect(screen.getAllByTestId('lb-row').length).toBe(2);
    expect(screen.getByText(/Falcon Value/)).toBeInTheDocument();
  });

  it('off-mock error → honest-empty, NEVER a wire fixture', async () => {
    getDirectionalLeaderboardMock.mockRejectedValue(new Error('offline'));
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getByTestId('lb-empty')).toBeInTheDocument());
    expect(screen.queryByTestId('lb-row')).toBeNull();
  });

  it('mock ON is UNCHANGED: does NOT call the directional reader', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<LeaderboardPage />);
    // mock ON serves the canonical wire fixture via getLeaderboard() — rows appear …
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBeGreaterThan(0));
    // … and the directional reader is never touched.
    expect(getDirectionalLeaderboardMock).not.toHaveBeenCalled();
  });

  // Gate-3 MAJOR 3: the DIRECTIONAL board must carry the backend's HONEST proof aggregate on a
  // roster-local proof_state — a "mixed" agent must render "mixed", NEVER an unearned "reproducible".
  it('directional row renders the honest MIXED proof badge (never a fabricated reproducible)', async () => {
    getDirectionalLeaderboardMock.mockResolvedValue(adaptDirectionalLeaderboard(WIRE_MIXED));
    render(<LeaderboardPage />);
    await waitFor(() => expect(screen.getAllByTestId('lb-row').length).toBe(2));
    // The PROOF cell of the mixed row (rank 1) shows a 'mixed' badge — not 'reproducible'. (source_mode
    // is 'replay' here → the SOURCE column renders a replay Badge, so data-variant="mixed" is
    // unambiguously the proof badge.)
    const mixedRowVariants = Array.from(
      screen.getAllByTestId('lb-row')[0].querySelectorAll('[data-variant]'),
    ).map((e) => e.getAttribute('data-variant'));
    expect(mixedRowVariants).toContain('mixed');
    expect(mixedRowVariants).not.toContain('reproducible');
    // The verified row (rank 2) still renders its EARNED single-mode proof claim verbatim.
    const verifiedRowVariants = Array.from(
      screen.getAllByTestId('lb-row')[1].querySelectorAll('[data-variant]'),
    ).map((e) => e.getAttribute('data-variant'));
    expect(verifiedRowVariants).toContain('verified');
  });
});

describe('adaptDirectionalLeaderboard — honest proof_state (gate-3 M3)', () => {
  it('preserves the backend honest "mixed" aggregate as proof_state (never an unearned "reproducible")', () => {
    const rows = adaptDirectionalLeaderboard(WIRE_MIXED);
    expect(rows[0].proof_state).toBe('mixed');
    expect(rows[1].proof_state).toBe('verified');
    // The SHARED ProofMode field is untouched (mock/global path): it still coerces the aggregate
    // "mixed" → 'reproducible'. The honesty lives on the roster-local proof_state, not proof_mode.
    expect(rows[0].proof_mode).toBe('reproducible');
    expect(rows[1].proof_mode).toBe('verified');
  });
});
