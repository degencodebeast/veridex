import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import { AGENTS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';

describe('AgentsScreen (REQ-017)', () => {
  it('links Compare Two -> Duel and Create Agent -> Studio', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('link', { name: /compare two/i })).toHaveAttribute('href', '/duel');
    expect(screen.getByRole('link', { name: /create agent/i })).toHaveAttribute('href', '/studio');
  });

  it('links each agent row to its profile', () => {
    // Directional roster is supplied explicitly (the demo path the page mock-gates); no fixture default.
    render(<AgentsScreen agents={AGENTS} />);
    expect(screen.getByRole('link', { name: /Value CLV/i })).toHaveAttribute('href', '/agents/value_clv');
  });

  it('filters by search text', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen agents={AGENTS} />);
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
