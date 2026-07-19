import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import { getMakerArenaResult } from '@/lib/api';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getMakerArenaResult: vi.fn(),
}));

const getMakerArenaResultMock = vi.mocked(getMakerArenaResult);

describe('AgentsScreen (REQ-017)', () => {
  it('links Compare Two -> Duel and Create Agent -> Studio', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('link', { name: /compare two/i })).toHaveAttribute('href', '/duel');
    expect(screen.getByRole('link', { name: /create agent/i })).toHaveAttribute('href', '/studio');
  });

  it('links each agent row to its profile', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('link', { name: /Value CLV/i })).toHaveAttribute('href', '/agents/value_clv');
  });

  it('filters by search text', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.type(screen.getByRole('searchbox'), 'momentum');
    expect(screen.getByRole('link', { name: /Momentum FR/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Value CLV/i })).toBeNull();
  });

  it('is honest when empty — renders no fabricated agent rows (REQ-017 honest states)', () => {
    render(<AgentsScreen agents={[]} />);
    const agentLinks = screen.queryAllByRole('link').filter((l) => l.getAttribute('href')?.startsWith('/agents/'));
    expect(agentLinks).toHaveLength(0);
    expect(screen.getByTestId('agents-empty')).toBeInTheDocument();
  });
});

describe('AgentsScreen — Maker Arena lane (MM-R1)', () => {
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
    getMakerArenaResultMock.mockResolvedValue(MAKER_ARENA_RESULT);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
  });

  it('defaults to the Directional lane — the existing CLV directory is untouched', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('radio', { name: 'Directional' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByTestId('maker-agent-row')).toBeNull();
  });

  it('the maker lane shows exactly the 2 maker agents (a separate population, SEC-005)', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const rows = screen.getAllByTestId('maker-agent-row');
    expect(rows.length).toBe(2);
    expect(rows.map((r) => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('txline-fair-mm'), expect.stringContaining('naive-mm')]),
    );
  });

  it('ranks the maker agents by toxicity loss ASC — never CLV, and carries no Avg CLV column', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const rows = screen.getAllByTestId('maker-agent-row');
    expect(within(rows[0]).getByTestId('maker-agent-link')).toHaveTextContent('txline-fair-mm');
    expect(within(rows[1]).getByTestId('maker-agent-link')).toHaveTextContent('naive-mm');
    expect(within(screen.getByRole('table')).queryByText(/avg clv/i)).toBeNull();
  });

  it('rows deep-link to the Maker Proof Card', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const links = screen.getAllByTestId('maker-agent-link');
    expect(links[0]).toHaveAttribute('href', '/proof/maker/txline-fair-mm');
    expect(links[1]).toHaveAttribute('href', '/proof/maker/naive-mm');
  });

  it('SEC-005: maker rows never carry a directional rank/CLV key', () => {
    for (const row of MAKER_ARENA_RESULT.leaderboard) {
      expect(Object.keys(row)).not.toContain('avg_clv_bps');
      expect(row.real_executable_edge_bps).toBeNull();
    }
  });

  // I-R remediation (Min5): the visible PROOF affordance must be a working accessible link,
  // not dead table-cell text beside a working agent-name link.
  it('Min5: the visible PROOF affordance is a real accessible link to the maker proof route', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const proofLinks = screen.getAllByRole('link', { name: /proof card for/i });
    expect(proofLinks).toHaveLength(2);
    for (const link of proofLinks) {
      expect(link.getAttribute('href')).toMatch(/^\/proof\/maker\/(txline-fair-mm|naive-mm)$/);
      expect(link).toHaveTextContent(/proof/i);
    }
  });
});

describe('AgentsScreen — live maker result loading (F-9)', () => {
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('loads the maker result through the API only after Maker mode opens', async () => {
    const liveResult = structuredClone(MAKER_ARENA_RESULT);
    liveResult.leaderboard[0].avg_toxicity_loss_bps = 7;
    getMakerArenaResultMock.mockResolvedValue(liveResult);
    const user = userEvent.setup();

    render(<AgentsScreen />);
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByText('+7.0 bps')).toBeInTheDocument();
    expect(getMakerArenaResultMock).toHaveBeenCalledTimes(1);
  });

  it('renders an honest unavailable state with zero maker rows when the API rejects', async () => {
    getMakerArenaResultMock.mockRejectedValue(new Error('maker endpoint offline'));
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByTestId('maker-unavailable')).toHaveTextContent(/maker data unavailable/i);
    expect(screen.queryAllByTestId('maker-agent-row')).toHaveLength(0);
  });

  it('renders an honest empty state when the API leaderboard has no maker agents', async () => {
    getMakerArenaResultMock.mockResolvedValue({ ...MAKER_ARENA_RESULT, leaderboard: [] });
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByTestId('maker-empty')).toHaveTextContent(/no maker results available/i);
    expect(screen.queryAllByTestId('maker-agent-row')).toHaveLength(0);
  });

  it('uses an explicitly injected maker result without making an API request', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen makerResult={MAKER_ARENA_RESULT} />);

    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(screen.getAllByTestId('maker-agent-row')).toHaveLength(2);
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();
  });

  it('prohibits direct sealed-fixture imports and defaults in all three production screens', () => {
    const sources = [
      'AgentsScreen.tsx',
      'DuelScreen.tsx',
      'LeaderboardScreen.tsx',
    ].map((file) => readFileSync(resolve(process.cwd(), 'components/screens', file), 'utf8'));

    for (const source of sources) {
      expect(source).not.toContain('MAKER_ARENA_RESULT');
    }
  });
});
