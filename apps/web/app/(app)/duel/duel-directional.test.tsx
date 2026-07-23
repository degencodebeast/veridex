// E4 · The Public-Agents lane: single fetch authority + single lane authority (DuelScreen), the real
// off-mock roster read (getAgentsRoster), hydration-safety, and async two-agent selection. The MAKER
// lane is a separate population — asserted untouched in duel-maker-regression.test.tsx.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderToString } from 'react-dom/server';
import { hydrateRoot } from 'react-dom/client';
import { getAgentsRoster, getMakerArenaResult } from '@/lib/api';
import { DuelScreen } from '@/components/screens/DuelScreen';
import DuelPage from './page';
import type { PublicAgentRow } from '@/lib/catalog';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getAgentsRoster: vi.fn(async () => []),
  getMakerArenaResult: vi.fn(async () => { throw new Error('maker endpoint offline'); }),
}));

const getAgentsRosterMock = vi.mocked(getAgentsRoster);
const getMakerArenaResultMock = vi.mocked(getMakerArenaResult);

const ROWS: PublicAgentRow[] = [
  { public_agent_id: 'pa_a', display_name: 'Alpha', owner_public_label: 'acme', origin: 'byoa', proof_state: 'reproducible', archetype: 'value_clv', mode: null, avg_clv_bps: 18.4, runs: 14, valid_pct: 95 },
  { public_agent_id: 'pa_b', display_name: 'Bravo', owner_public_label: 'demo', origin: 'unknown', proof_state: 'verified', archetype: 'baseline', mode: null, avg_clv_bps: 6.2, runs: 14, valid_pct: 96 },
  { public_agent_id: 'pa_c', display_name: 'Charlie', owner_public_label: 'demo', origin: 'unknown', proof_state: 'unscored', archetype: 'momentum', mode: null, avg_clv_bps: null, runs: null, valid_pct: null },
];

afterEach(() => {
  getAgentsRosterMock.mockReset();
  getAgentsRosterMock.mockResolvedValue([]);
  getMakerArenaResultMock.mockReset();
  getMakerArenaResultMock.mockRejectedValue(new Error('maker endpoint offline'));
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/');
});

describe('E4 fetch ownership — the roster read is the SCREEN, gated correctly', () => {
  it('direct ?lane=maker: ZERO getAgentsRoster reads (maker never touches the roster)', async () => {
    window.history.replaceState(null, '', '/?lane=maker');
    const user = userEvent.setup();
    render(<DuelScreen mockResolved mockAgents={null} />);
    // the maker lane is active from the URL; give effects a tick
    await screen.findByRole('radio', { name: 'Maker' });
    await waitFor(() => expect(getMakerArenaResultMock).toHaveBeenCalled());
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
    void user;
  });

  it('Maker → Public Agents triggers exactly ONE roster read; toggling back never refetches', async () => {
    window.history.replaceState(null, '', '/?lane=maker');
    getAgentsRosterMock.mockResolvedValue(ROWS);
    const user = userEvent.setup();
    render(<DuelScreen mockResolved mockAgents={null} />);
    expect(getAgentsRosterMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole('radio', { name: 'Public Agents' }));
    await waitFor(() => expect(getAgentsRosterMock).toHaveBeenCalledTimes(1));

    // toggle Maker → Public Agents again: the single read is not repeated
    await user.click(screen.getByRole('radio', { name: 'Maker' }));
    await user.click(screen.getByRole('radio', { name: 'Public Agents' }));
    expect(getAgentsRosterMock).toHaveBeenCalledTimes(1);
  });

  it('mock ON (injected rows) → the real reader is NEVER called', () => {
    render(<DuelScreen mockResolved mockAgents={ROWS} />);
    expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument();
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
  });

  it('unresolved mock gate → NO roster read (stable shell, no fetch)', () => {
    render(<DuelScreen mockResolved={false} mockAgents={null} />);
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
    expect(getAgentsRosterMock).not.toHaveBeenCalled();
  });
});

describe('E4 data-source — off-mock honesty', () => {
  it('off-mock ≥2 distinct rows → populates the compare (real display names, never a fixture)', async () => {
    getAgentsRosterMock.mockResolvedValue(ROWS);
    render(<DuelScreen mockResolved mockAgents={null} />);
    await waitFor(() => expect(screen.getByLabelText(/agent a/i)).toBeInTheDocument());
    // display names appear in both the <option> and the card heading → at least one occurrence
    expect(screen.getAllByText('Alpha').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Bravo').length).toBeGreaterThan(0);
  });

  it('off-mock empty roster (0) → honest-empty compare', async () => {
    getAgentsRosterMock.mockResolvedValue([]);
    render(<DuelScreen mockResolved mockAgents={null} />);
    await waitFor(() => expect(getAgentsRosterMock).toHaveBeenCalled());
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
    expect(screen.queryByLabelText(/agent a/i)).toBeNull();
  });

  it('off-mock single row (1) → honest-empty (needs ≥2 DISTINCT ids)', async () => {
    getAgentsRosterMock.mockResolvedValue([ROWS[0]]);
    render(<DuelScreen mockResolved mockAgents={null} />);
    await waitFor(() => expect(getAgentsRosterMock).toHaveBeenCalled());
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
  });

  it('off-mock fetch failure still resolves to [] → honest-empty (getAgentsRoster catches internally)', async () => {
    getAgentsRosterMock.mockResolvedValue([]); // the reader itself returns [] on error
    render(<DuelScreen mockResolved mockAgents={null} />);
    await waitFor(() => expect(getAgentsRosterMock).toHaveBeenCalled());
    expect(screen.getByTestId('duel-empty')).toBeInTheDocument();
  });
});

describe('E4 hydration — server + first client render identical (no mismatch)', () => {
  it('under ?mock=1: SSR shell == first client render (mockResolved=false), rows appear only after the effect', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const html = renderToString(<DuelPage />);
    // the server render is the unresolved honest-empty shell — no agents, no fetch
    expect(html).toMatch(/Public Agents/);
    expect(html).not.toMatch(/Momentum FR|Value CLV/); // no demo rows in the SSR shell

    const container = document.createElement('div');
    container.innerHTML = html;
    document.body.appendChild(container);

    const errors: string[] = [];
    const spy = vi.spyOn(console, 'error').mockImplementation((...a: unknown[]) => { errors.push(a.map(String).join(' ')); });
    await act(async () => {
      hydrateRoot(container, <DuelPage />);
    });
    // no hydration mismatch was logged
    expect(errors.join(' ')).not.toMatch(/hydrat|did not match|mismatch/i);
    spy.mockRestore();

    // the demo rows arrive only after the mount effect resolves the gate
    await waitFor(() => expect(container.textContent).toMatch(/Momentum FR|Value CLV/));
    document.body.removeChild(container);
  });
});

describe('E4 async selection — two distinct agents, always valid', () => {
  it('async arrival seeds two DISTINCT public ids; neither select is empty', async () => {
    getAgentsRosterMock.mockResolvedValue(ROWS);
    render(<DuelScreen mockResolved mockAgents={null} />);
    const a = (await screen.findByLabelText(/agent a/i)) as HTMLSelectElement;
    const b = screen.getByLabelText(/agent b/i) as HTMLSelectElement;
    expect(a.value).toBeTruthy();
    expect(b.value).toBeTruthy();
    expect(a.value).not.toBe(b.value);
  });

  it('a valid selection survives a roster refresh with the SAME ids', async () => {
    const { rerender } = render(<DuelScreen mockResolved mockAgents={ROWS} />);
    const user = userEvent.setup();
    // default A=pa_a, B=pa_b; A's options exclude pa_b, so move A to a still-available distinct id
    await user.selectOptions(screen.getByLabelText(/agent a/i), 'pa_c');
    expect((screen.getByLabelText(/agent a/i) as HTMLSelectElement).value).toBe('pa_c');
    // a refetch returns a NEW array with identical ids → the selection is preserved
    rerender(<DuelScreen mockResolved mockAgents={ROWS.map((r) => ({ ...r }))} />);
    expect((screen.getByLabelText(/agent a/i) as HTMLSelectElement).value).toBe('pa_c');
  });

  it('a removed selection is replaced with a valid distinct agent', async () => {
    const { rerender } = render(<DuelScreen mockResolved mockAgents={ROWS} />);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText(/agent a/i), 'pa_c');
    expect((screen.getByLabelText(/agent a/i) as HTMLSelectElement).value).toBe('pa_c');
    // pa_c is removed from the roster → the selection must fall back to a still-present id
    rerender(<DuelScreen mockResolved mockAgents={[ROWS[0], ROWS[1]]} />);
    const a = screen.getByLabelText(/agent a/i) as HTMLSelectElement;
    const b = screen.getByLabelText(/agent b/i) as HTMLSelectElement;
    expect(['pa_a', 'pa_b']).toContain(a.value);
    expect(a.value).not.toBe(b.value);
  });
});
