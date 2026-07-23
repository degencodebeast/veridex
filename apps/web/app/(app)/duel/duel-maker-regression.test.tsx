// E4 · Maker-lane regression: de-faking the Public-Agents lane must leave the Maker duel EXACTLY as it
// was. The fixed falsification pairing still loads, both maker agents render, the toxicity/Δ/CI verdict
// is intact, and the Maker lane never touches the public roster reader.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { getAgentsRoster, getMakerArenaResult } from '@/lib/api';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getAgentsRoster: vi.fn(async () => []),
  getMakerArenaResult: vi.fn(),
}));

const getAgentsRosterMock = vi.mocked(getAgentsRoster);
const getMakerArenaResultMock = vi.mocked(getMakerArenaResult);

beforeEach(() => {
  getAgentsRosterMock.mockReset();
  getAgentsRosterMock.mockResolvedValue([]);
  getMakerArenaResultMock.mockReset();
  getMakerArenaResultMock.mockResolvedValue(MAKER_ARENA_RESULT);
});

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('E4 Maker lane preserved after the Public-Agents de-fake', () => {
  it('selecting Maker loads the sealed maker result — both fixed agents render', async () => {
    const user = userEvent.setup();
    render(<DuelScreen makerResult={MAKER_ARENA_RESULT} />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const cards = screen.getAllByTestId('duel-maker-card');
    expect(cards).toHaveLength(2);
    const text = cards.map((c) => c.textContent).join(' ');
    expect(text).toContain('txline-fair-mm');
    expect(text).toContain('naive-mm');
  });

  it('toxicity loss + Δ quote-quality + 95% CI are intact', async () => {
    const user = userEvent.setup();
    render(<DuelScreen makerResult={MAKER_ARENA_RESULT} />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    const toxicities = screen.getAllByTestId('duel-maker-toxicity').map((el) => el.textContent);
    expect(toxicities).toEqual(expect.arrayContaining([expect.stringContaining('129'), expect.stringContaining('172')]));
    const result = screen.getByTestId('duel-result');
    expect(within(result).getByText(/separated/i)).toBeInTheDocument();
    expect(within(result).getByText(/\+43/)).toBeInTheDocument();
    expect(within(result).getByText('[34, 52]')).toBeInTheDocument();
  });

  it('the Maker lane never reads the public roster', async () => {
    const user = userEvent.setup();
    render(<DuelScreen makerResult={MAKER_ARENA_RESULT} />);
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    expect(screen.getAllByTestId('duel-maker-card')).toHaveLength(2);
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
  });

  it('a direct ?lane=maker load renders the maker duel without a roster read', async () => {
    window.history.replaceState(null, '', '/?lane=maker');
    render(<DuelScreen mockResolved mockAgents={null} makerResult={MAKER_ARENA_RESULT} />);
    expect(await screen.findAllByTestId('duel-maker-card')).toHaveLength(2);
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
  });
});
