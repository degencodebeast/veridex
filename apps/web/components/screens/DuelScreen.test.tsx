import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { getMakerArenaResult } from '@/lib/api';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
import {
  MAKER_INCONCLUSIVE, MAKER_INVERTED, MAKER_UNKNOWN_VERDICT, makerResultWith,
} from '../../__tests__/fixtures/makerVariants';
import type { MakerArenaResultView } from '@/lib/contracts';
import type { PublicAgentRow } from '@/lib/catalog';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getMakerArenaResult: vi.fn(),
  getAgentsRoster: vi.fn(),
}));

const getMakerArenaResultMock = vi.mocked(getMakerArenaResult);

// Explicit PublicAgentRow fixtures — the honest public identity contract the Public-Agents lane renders.
// First two are numeric (default A/B) so the difference is a real number; the third is a null-perf,
// UNSCORED public row (renders "—", never a fabricated 0). Origins avoid the 'official' token so the
// "no official-or-verified claim" negative is a claim-scoped assertion, not a factual-field false-trip.
const ROWS: PublicAgentRow[] = [
  { public_agent_id: 'pa_val', display_name: 'Value CLV', owner_public_label: 'acme', origin: 'byoa', proof_state: 'reproducible', archetype: 'value_clv', mode: 'numeric', avg_clv_bps: 18.4, runs: 14, valid_pct: 95.0 },
  { public_agent_id: 'pa_base', display_name: 'Baseline', owner_public_label: 'demo', origin: 'unknown', proof_state: 'verified', archetype: 'baseline', mode: 'numeric', avg_clv_bps: 6.2, runs: 14, valid_pct: 96.4 },
  { public_agent_id: 'pa_mom', display_name: 'Momentum FR', owner_public_label: 'demo', origin: 'unknown', proof_state: 'unscored', archetype: 'momentum', mode: null, avg_clv_bps: null, runs: null, valid_pct: null },
];

afterEach(() => {
  window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
});

describe('DuelScreen — Public Agents · Summary Comparison (off-mock, honest labels)', () => {
  it('unresolved shell (mock gate not resolved): honest-empty, no cards, no selectors', () => {
    render(<DuelScreen mockResolved={false} mockAgents={null} />);
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
    expect(screen.queryAllByTestId('duel-card')).toHaveLength(0);
    expect(screen.queryByLabelText(/agent a/i)).toBeNull();
  });

  it('fewer than two DISTINCT public agents → honest-empty, no crash', () => {
    render(<DuelScreen mockResolved mockAgents={[ROWS[0]]} />);
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
    expect(screen.queryAllByTestId('duel-card')).toHaveLength(0);
  });

  it('the VISIBLE segmented option reads "Public Agents", never "Directional"', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    expect(screen.getByRole('radio', { name: 'Public Agents' })).toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: 'Directional' })).toBeNull();
  });

  it('heading is the factual summary title (not Official / Directional / a controlled head-to-head)', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    expect(screen.getByRole('heading', { name: 'Public Agents · Summary Comparison' })).toBeInTheDocument();
  });

  it('renders both display names + pooled Avg CLV + runs + valid % + proof state + origin + owner', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    const cards = screen.getAllByTestId('duel-card');
    expect(cards).toHaveLength(2);
    // default A = first numeric, B = second numeric
    expect(within(cards[0]).getByText('Value CLV')).toBeInTheDocument();
    expect(within(cards[1]).getByText('Baseline')).toBeInTheDocument();
    cards.forEach((c) => {
      expect(within(c).getByText(/pooled avg clv/i)).toBeInTheDocument();
      expect(within(c).getByText(/runs/i)).toBeInTheDocument();
      expect(within(c).getByText(/valid %/i)).toBeInTheDocument();
      expect(within(c).getByTestId('duel-proof')).toBeInTheDocument();
      expect(within(c).getByText(/origin/i)).toBeInTheDocument();
      expect(within(c).getByText(/owner/i)).toBeInTheDocument();
    });
  });

  it('public_agent_id drives identity — the selects carry the public ids, never operator identifiers', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    const a = screen.getByLabelText(/agent a/i) as HTMLSelectElement;
    const b = screen.getByLabelText(/agent b/i) as HTMLSelectElement;
    expect(a.value).toBe('pa_val');
    expect(b.value).toBe('pa_base');
    // options are keyed on public ids only
    const optionValues = Array.from(a.querySelectorAll('option')).map((o) => (o as HTMLOptionElement).value);
    expect(optionValues.every((v) => v.startsWith('pa_'))).toBe(true);
  });

  it('null pooled performance renders "—", never a fabricated 0', async () => {
    const user = userEvent.setup();
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    await user.selectOptions(screen.getByLabelText(/agent b/i), 'pa_mom');
    const cards = screen.getAllByTestId('duel-card');
    const bClv = within(cards[1]).getByTestId('duel-clv');
    expect(bClv).toHaveTextContent('—');
    expect(bClv).not.toHaveTextContent('0');
  });

  it('difference is labeled "Pooled Avg CLV difference" — a real number when both numeric, "—" when either null', async () => {
    const user = userEvent.setup();
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    // default A=18.4, B=6.2 → 12.2
    expect(screen.getByText(/pooled avg clv difference/i)).toHaveTextContent('12.2');
    await user.selectOptions(screen.getByLabelText(/agent b/i), 'pa_mom');
    expect(screen.getByText(/pooled avg clv difference/i)).toHaveTextContent('—');
  });

  it('switching one side never copies the other agent perf — independent recompute', async () => {
    const user = userEvent.setup();
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    // default A=pa_val, B=pa_base; A's options exclude B's id, so pick a still-available distinct id
    await user.selectOptions(screen.getByLabelText(/agent a/i), 'pa_mom');
    const cards = screen.getAllByTestId('duel-card');
    const leftClv = within(cards[0]).getByTestId('duel-clv').textContent;
    const rightClv = within(cards[1]).getByTestId('duel-clv').textContent;
    expect(leftClv).not.toBe(rightClv);
  });

  it('SAME agent both sides is prevented — each select excludes the other side\'s id', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    const a = screen.getByLabelText(/agent a/i) as HTMLSelectElement;
    const b = screen.getByLabelText(/agent b/i) as HTMLSelectElement;
    expect(a.value).not.toBe(b.value);
    const aOptions = Array.from(a.querySelectorAll('option')).map((o) => (o as HTMLOptionElement).value);
    const bOptions = Array.from(b.querySelectorAll('option')).map((o) => (o as HTMLOptionElement).value);
    // B can never be set to A's id, nor A to B's id — the pair can never collapse to one agent.
    expect(bOptions).not.toContain(a.value);
    expect(aOptions).not.toContain(b.value);
  });

  it('duplicate public_agent_id rows are de-duplicated — one option per distinct id', () => {
    const dup = [ROWS[0], ROWS[0], ROWS[1]];
    render(<DuelScreen mockResolved mockAgents={dup} />);
    const a = screen.getByLabelText(/agent a/i) as HTMLSelectElement;
    const b = screen.getByLabelText(/agent b/i) as HTMLSelectElement;
    expect(a.value).not.toBe(b.value);
    const allValues = Array.from(a.querySelectorAll('option')).map((o) => (o as HTMLOptionElement).value);
    expect(new Set(allValues).size).toBe(allValues.length); // no duplicate options
  });

  it('NO fabricated evidence / eligibility / directional-or-controlled claims (scoped to the Public-Agents render)', () => {
    const { container } = render(<DuelScreen mockResolved mockAgents={ROWS} />);
    // the whole render IS the Public-Agents lane (no maker content present) — container is the scope
    expect(within(container).queryByTestId('evidence-hash')).toBeNull();
    expect(container.textContent).not.toMatch(/SAME SEALED EVIDENCE|identical sealed evidence|evidence_hash|0xseal/i);
    expect(container.textContent).not.toMatch(/controlled head-to-head/i);
    expect(container.textContent).not.toMatch(/shared evidence|same sealed/i);
    expect(within(container).queryByText(/eligibility/i)).toBeNull();
    expect(within(container).queryByText(/winner|\bwins\b|beats|champion/i)).toBeNull();
    expect(container.textContent).not.toMatch(/\bDirectional\b|\bOfficial\b|verified head-to-head/);
    // no anchored badge on the public-agents card
    expect(container.querySelector('[data-variant="anchored"]')).toBeNull();
  });
});

describe('DuelScreen — Maker Arena lane (MM-R1)', () => {
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
    getMakerArenaResultMock.mockResolvedValue(MAKER_ARENA_RESULT);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/'); // reset any ?lane= query between tests
  });

  it('defaults to the Public Agents lane — the summary compare (agent picker) is untouched', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    expect(screen.getByRole('radio', { name: 'Public Agents' })).toHaveAttribute('aria-checked', 'true');
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
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
  });

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
    expect(within(result).getAllByText(/reliably more toxic|reliably worse|inverted/i).length).toBeGreaterThan(0);
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

describe('DuelScreen — live maker result loading (F-9)', () => {
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('loads the fixed maker comparison through the API only after Maker mode opens', async () => {
    const liveResult = structuredClone(MAKER_ARENA_RESULT);
    liveResult.leaderboard[0].avg_toxicity_loss_bps = 7;
    getMakerArenaResultMock.mockResolvedValue(liveResult);
    const user = userEvent.setup();

    render(<DuelScreen />);
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByText('+7.0 bps')).toBeInTheDocument();
    expect(getMakerArenaResultMock).toHaveBeenCalledTimes(1);
  });

  it('renders an honest unavailable state with zero maker cards when the API rejects', async () => {
    getMakerArenaResultMock.mockRejectedValue(new Error('maker endpoint offline'));
    const user = userEvent.setup();

    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByTestId('maker-unavailable')).toHaveTextContent(/maker data unavailable/i);
    expect(screen.queryAllByTestId('duel-maker-card')).toHaveLength(0);
  });

  it('renders an honest unavailable-comparison state for an empty maker leaderboard', async () => {
    getMakerArenaResultMock.mockResolvedValue({ ...MAKER_ARENA_RESULT, leaderboard: [] });
    const user = userEvent.setup();

    render(<DuelScreen />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(await screen.findByTestId('maker-empty')).toHaveTextContent(/maker comparison unavailable/i);
    expect(screen.queryAllByTestId('duel-maker-card')).toHaveLength(0);
  });

  it('uses an explicitly injected maker result without making an API request', async () => {
    const user = userEvent.setup();
    render(<DuelScreen makerResult={MAKER_ARENA_RESULT} />);

    await user.click(screen.getByRole('radio', { name: 'Maker' }));

    expect(screen.getAllByTestId('duel-maker-card')).toHaveLength(2);
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();
  });
});
