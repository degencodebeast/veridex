import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { AGENTS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
import {
  MAKER_INCONCLUSIVE, MAKER_INVERTED, MAKER_UNKNOWN_VERDICT, makerResultWith,
} from '../../__tests__/fixtures/makerVariants';
import type { MakerArenaResultView } from '@/lib/contracts';

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

// I-R remediation (M1 / M2 / M4): the maker duel must render the REAL sealed verdict and the
// REAL per-agent counts, and may never assert a proof state the data does not carry.
describe('DuelScreen — Maker duel verdict/anchor/count honesty (I-R M1, M2, M4)', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
  });

  async function openMakerLane(result: MakerArenaResultView) {
    const user = userEvent.setup();
    const view = render(<DuelScreen makerResult={result} />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    return view;
  }

  function cardOf(agentId: string) {
    const card = screen.getAllByTestId('duel-maker-card').find((c) => c.textContent!.includes(agentId));
    expect(card, `duel card for ${agentId}`).toBeTruthy();
    return card!;
  }

  it('M1: INCONCLUSIVE crowns NO winner — no LESS TOXIC chip on either card, no separation claim', async () => {
    await openMakerLane(MAKER_INCONCLUSIVE);
    expect(screen.queryByText(/less toxic/i)).toBeNull();
    const result = screen.getByTestId('duel-result');
    expect(within(result).getByText('Inconclusive')).toBeInTheDocument();
    expect(within(result).getByText(/no separation/i)).toBeInTheDocument();
    expect(within(result).queryByText(/whole 95% ci above zero|difference is real/i)).toBeNull();
  });

  it('M1: INVERTED is never rendered as a candidate win — Inverted badge, candidate not "less toxic"', async () => {
    await openMakerLane(MAKER_INVERTED);
    const result = screen.getByTestId('duel-result');
    expect(within(result).getByText('Inverted')).toBeInTheDocument();
    expect(within(result).queryByText(/whole 95% ci above zero|difference is real/i)).toBeNull();
    // the candidate card must NOT be presented as the less-toxic side
    expect(within(cardOf('txline-fair-mm')).queryByText(/less toxic/i)).toBeNull();
    // the inverted headline must state the candidate is reliably worse, not separated/safer
    expect(within(result).getByText(/reliably more toxic|reliably worse|inverted/i)).toBeInTheDocument();
    expect(within(result).queryByText(/txline-fair-mm is less toxic/i)).toBeNull();
  });

  it('M1: an out-of-vocabulary verdict renders an honest no-claim state — never a winner', async () => {
    await openMakerLane(MAKER_UNKNOWN_VERDICT);
    expect(screen.queryByText(/less toxic/i)).toBeNull();
    const result = screen.getByTestId('duel-result');
    expect(within(result).queryByText(/difference is real|whole 95% ci above zero/i)).toBeNull();
  });

  it('M1: SEPARATED still crowns the candidate — the LESS TOXIC chip sits on the candidate card only', async () => {
    await openMakerLane(MAKER_ARENA_RESULT);
    expect(within(cardOf('txline-fair-mm')).getByText(/less toxic/i)).toBeInTheDocument();
    expect(within(cardOf('naive-mm')).queryByText(/less toxic/i)).toBeNull();
  });

  it('M2: the maker duel never claims Anchored — a sealed tape is not an external anchor', async () => {
    const { container } = await openMakerLane(MAKER_ARENA_RESULT);
    expect(container.querySelector('[data-variant="anchored"]')).toBeNull();
    expect(container.querySelector('[data-variant="not-anchored"]')).not.toBeNull();
  });

  it('M4: the scored cell renders row.scored, never fixture_universe_n', async () => {
    const res = makerResultWith({ scoredByAgent: { 'txline-fair-mm': 15, 'naive-mm': 12 } });
    await openMakerLane(res);
    expect(cardOf('txline-fair-mm').textContent).toContain('0 · 15');
    expect(cardOf('naive-mm').textContent).toContain('0 · 12');
    expect(cardOf('txline-fair-mm').textContent).not.toContain('0 · 18');
  });

  it('M4: the sealed fixture itself has scored ≠ fixture_universe_n — the real per-agent count renders', async () => {
    await openMakerLane(MAKER_ARENA_RESULT);
    const candidate = MAKER_ARENA_RESULT.leaderboard.find((r) => r.agent_id === 'txline-fair-mm')!;
    expect(candidate.scored).not.toBe(MAKER_ARENA_RESULT.fixture_universe_n);
    // target the labeled cell itself (quote_count == scored in the sealed fixture, so a
    // whole-card textContent match would pass vacuously through the Quotes cell)
    const label = within(cardOf('txline-fair-mm')).getByText('Abstained · Scored');
    expect(label.nextElementSibling?.textContent).toBe(`${candidate.abstained} · ${candidate.scored.toLocaleString()}`);
  });
});
