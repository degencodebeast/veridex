import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { AGENTS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';

describe('DuelScreen (REQ-023)', () => {
  it('guards against fewer than two agents — honest empty, no crash', () => {
    render(<DuelScreen agents={[AGENTS[0]]} />);
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
    expect(screen.queryAllByTestId('duel-card')).toHaveLength(0);
  });

  it('shows two agents on the SAME sealed evidence (one shared evidence hash)', () => {
    render(<DuelScreen />);
    const evidence = screen.getAllByTestId('evidence-hash');
    expect(evidence.length).toBe(1);
    expect(evidence[0]).toHaveTextContent(/sealed evidence/i);
  });

  it('compares CLV and proof side-by-side for the two selected agents', () => {
    render(<DuelScreen />);
    const cards = screen.getAllByTestId('duel-card');
    expect(cards.length).toBe(2);
    cards.forEach((c) => {
      expect(within(c).getByText(/avg clv/i)).toBeInTheDocument();
      expect(within(c).getByTestId('duel-proof')).toBeInTheDocument();
    });
  });

  it('lets the operator switch one side without copying the other agent CLV', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    const left = screen.getByLabelText(/agent a/i);
    await user.selectOptions(left, 'baseline');
    const cards = screen.getAllByTestId('duel-card');
    const leftClv = within(cards[0]).getByTestId('duel-clv').textContent;
    const rightClv = within(cards[1]).getByTestId('duel-clv').textContent;
    expect(leftClv).not.toBe(rightClv); // independent recompute per agent
  });

  it('is an honest factual compare — no fabricated winner/edge, just the CLV gap on shared evidence', () => {
    render(<DuelScreen />);
    expect(screen.queryByText(/winner|\bwins\b|beats|champion|🏆/i)).toBeNull();
    expect(screen.getByText(/key divergence/i)).toBeInTheDocument();
    expect(screen.getByText(/identical sealed evidence/i)).toBeInTheDocument();
  });
});

describe('DuelScreen — Maker Arena lane (MM-R1)', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
  });

  it('defaults to the Directional lane — the existing CLV duel (agent picker) is untouched', () => {
    render(<DuelScreen />);
    expect(screen.getByRole('radio', { name: 'Directional' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument();
    expect(screen.queryAllByTestId('duel-maker-card')).toHaveLength(0);
  });

  it('the maker lane runs the FIXED naive-mm vs txline-fair-mm pairing — no agent picker', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    expect(screen.queryByLabelText(/agent a/i)).toBeNull();
    const cards = screen.getAllByTestId('duel-maker-card');
    expect(cards.length).toBe(2);
    expect(cards.map((c) => c.textContent).join(' ')).toContain('txline-fair-mm');
    expect(cards.map((c) => c.textContent).join(' ')).toContain('naive-mm');
  });

  it('headline metric is toxicity loss (never CLV) and exec edge renders the literal null', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const toxicities = screen.getAllByTestId('duel-maker-toxicity').map((el) => el.textContent);
    expect(toxicities).toEqual(expect.arrayContaining([expect.stringContaining('129'), expect.stringContaining('172')]));
    screen.getAllByTestId('duel-maker-edge').forEach((cell) => expect(cell).toHaveTextContent('null'));
  });

  it('DUEL RESULT shows the SEPARATED/INCONCLUSIVE verdict + Δ quote-quality bps + 95% CI', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const result = screen.getByTestId('duel-result');
    expect(within(result).getByText(/separated/i)).toBeInTheDocument();
    expect(within(result).getByText(/\+43/)).toBeInTheDocument();
    expect(within(result).getByText('[34, 52]')).toBeInTheDocument();
  });

  it('per-quote panel is honest-empty — not yet surfaced by the API (future)', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    expect(screen.getByTestId('duel-per-quote-empty')).toHaveTextContent(/not yet surfaced by the api/i);
  });

  it('SEC-005: the maker duel never carries a directional rank/CLV key', () => {
    for (const row of MAKER_ARENA_RESULT.leaderboard) {
      expect(Object.keys(row)).not.toContain('avg_clv_bps');
    }
  });
});
