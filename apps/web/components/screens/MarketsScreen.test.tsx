import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import type { OddsUpdate } from '@/lib/catalog';

afterEach(() => { vi.unstubAllEnvs(); });

// In-running-only odds (no pre-match capture) → closings cannot be reconstructed → pending/—
// (the honest CON-040 branch). Covers all three soccer families.
const IN_RUNNING: Record<number, OddsUpdate[]> = {
  18172280: [
    { fixture_id: 18172280, message_id: 'a', ts: 1, in_running: true, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['FRA', 'Draw', 'BRA'], prices: [1472, 3550, 6100], pct: ['67.935', '28.169', '16.393'] },
    { fixture_id: 18172280, message_id: 'b', ts: 2, in_running: true, market_family: 'OVERUNDER_PARTICIPANT_GOALS', market_parameters: 'line=2.5', price_names: ['Over', 'Under'], prices: [1910, 1980], pct: ['52.356', '50.505'] },
    { fixture_id: 18172280, message_id: 'c', ts: 3, in_running: true, market_family: 'ASIANHANDICAP_PARTICIPANT_GOALS', market_parameters: 'line=-0.25', price_names: ['FRA', 'BRA'], prices: [1880, 2010], pct: ['53.191', '49.751'] },
  ],
};

describe('MarketsScreen (REQ-016 / AC-010/011 / REQ-042 / CON-040)', () => {
  it('marks Soccer active and US College FB/BB disabled with a "not in free feed" label (AC-011)', () => {
    render(<MarketsScreen />);
    expect(screen.getByRole('button', { name: /Soccer/ })).not.toBeDisabled();
    const cfb = screen.getByRole('button', { name: /US College Football/ });
    expect(cfb).toBeDisabled();
    expect(screen.getAllByText(/not in free feed/i).length).toBeGreaterThanOrEqual(1);
  });

  it('reads odds from /odds/updates, never /odds/snapshot (AC-010)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const panel = screen.getByTestId('families');
    expect(panel.getAttribute('data-odds-path')).toBe('/odds/updates/18172280');
    expect(panel.getAttribute('data-odds-path')).not.toContain('snapshot');
  });

  it('reflects the ACTUAL source in the strip — replay/demo data is not mislabelled "live" (honest provenance)', async () => {
    const user = userEvent.setup();
    // default source is the honest replay/demo state (fixtures are not a live feed)
    const { unmount } = render(<MarketsScreen />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const strip = screen.getByTestId('source-strip');
    expect(strip).toHaveTextContent(/replay/i);
    expect(strip).not.toHaveTextContent(/\blive\b/i);
    unmount();
    // only when the source is genuinely live does the strip say "live"
    render(<MarketsScreen sourceMode="live" />);
    await user.click(screen.getByTestId('fixture-18172280'));
    expect(screen.getByTestId('source-strip')).toHaveTextContent(/\blive\b/i);
  });

  it('renders the three families with decimal odds + implied % and pending/— closings (REQ-042/CON-040)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen oddsByFixture={IN_RUNNING} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    expect(within(fam).getByText(/Match Result/i)).toBeInTheDocument();
    expect(within(fam).getByText(/Over \/ Under/i)).toBeInTheDocument();
    expect(within(fam).getByText(/Asian Handicap/i)).toBeInTheDocument();
    expect(within(fam).getByText('1.472')).toBeInTheDocument(); // decimal odds (decoded, 3dp)
    expect(within(fam).getByText(/67\.935/)).toBeInTheDocument(); // full-precision implied %
    // in-running with no pre-match → CLOSING is pending (CON-040). Scope to /pending/ so this
    // tests the closing state, not the EDGE/AGENTS "—" cells (which are honestly — by design).
    expect(within(fam).getAllByText(/pending/i).length).toBeGreaterThanOrEqual(1);
  });

  it('does NOT render any unsupported TxLINE field — only the 4-field decimal outcome (#5 / REQ-042)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen oddsByFixture={IN_RUNNING} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    // No American odds / point-spread / depth-liquidity / per-bookmaker / possession-style stats.
    expect(within(fam).queryByText(/moneyline|american|spread|handicap line price|depth|liquidity|book(maker)?|possession|xg|corners/i)).toBeNull();
    // The visible column headers are exactly the supported decimal-odds set.
    expect(within(fam).getAllByText(/IMPLIED %/i).length).toBeGreaterThanOrEqual(1);
    expect(within(fam).getAllByText(/CLOSING/i).length).toBeGreaterThanOrEqual(1);
  });

  it('reconstructs a real closing value from pre-match updates (CON-040 value branch)', async () => {
    const user = userEvent.setup();
    // pre-match update (in_running:false) → closing reconstructable to a decimal value.
    const preMatch: Record<number, OddsUpdate[]> = {
      18172280: [
        { fixture_id: 18172280, message_id: 'p', ts: 1, in_running: false, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['FRA', 'Draw', 'BRA'], prices: [1500, 3500, 6000], pct: ['66.667', '28.571', '16.667'] },
      ],
    };
    render(<MarketsScreen oddsByFixture={preMatch} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    expect(within(fam).getAllByText('1.500').length).toBeGreaterThanOrEqual(1); // decimal AND closing both 1.500
    // closing is a value, not pending. Scope to /pending/ (not /—/) — the EDGE/AGENTS cells are
    // honestly "—" by design, which is unrelated to the closing-reconstruction branch under test.
    expect(within(fam).queryAllByText(/pending/i)).toHaveLength(0);
  });
});

// ── V5 fidelity: default-select + right rail + odds-table density ──────────────
describe('MarketsScreen V5 (default-select · right rail · EDGE/AGENTS honesty)', () => {
  it('default-selects the first fixture on load — the odds table populates WITHOUT a click', () => {
    render(<MarketsScreen />);
    // No interaction: the families table is already rendered (not the empty "select a fixture" prompt).
    const fam = screen.getByTestId('families');
    // consensus AND closing both reconstruct to 1.472 in the default data → at least one cell present.
    expect(within(fam).getAllByText('1.472').length).toBeGreaterThanOrEqual(1); // default fixture populated
    expect(screen.queryByText(/select a fixture/i)).toBeNull();
  });

  it('MATCH STATE rail shows the match-phase (IN-PLAY) — SEPARATE from source_mode, no source vocab', () => {
    render(<MarketsScreen />); // default fixture FRA v BRA is in_running → IN-PLAY
    const rail = screen.getByTestId('rail-match-state');
    expect(rail).toHaveTextContent(/FRA/);
    expect(rail).toHaveTextContent(/World Cup/);
    expect(rail).toHaveTextContent(/in-play/i);   // match phase (the fixture axis)
    // match-phase is NOT a data-source claim — the source axis lives in the strip/status bar.
    expect(rail).not.toHaveTextContent(/replay/i);
    expect(screen.getByTestId('source-strip')).toHaveTextContent(/replay/i);
  });

  it('FEED HEALTH rail renders honestly — not-live feed shows OFFLINE + REAL staleness, never "healthy/live"', () => {
    render(<MarketsScreen />); // catalog FEED_HEALTH default: ws_live=false, staleness_s=5
    const rail = screen.getByTestId('rail-feed-health');
    expect(rail).toHaveTextContent(/offline/i);   // ws_live=false → OFFLINE, never a fake "LIVE"
    expect(rail).not.toHaveTextContent(/\blive\b/i);
    expect(rail).not.toHaveTextContent(/healthy/i);
    expect(rail).toHaveTextContent(/5\s*s/);       // real staleness from the fixture
  });

  it('ELIGIBLE AGENTS rail shows the eligible POOL (not fixture-scoped) — not-eligible agents excluded', () => {
    render(<MarketsScreen />);
    const rail = screen.getByTestId('rail-eligible-agents');
    expect(rail).toHaveTextContent(/Value CLV/);   // eligible
    expect(rail).not.toHaveTextContent(/Momentum FR/); // not-eligible → excluded (SEC: eligibility honest)
    expect(rail).toHaveTextContent(/pool/i);       // honestly labeled a pool, NOT "scoped to this fixture"
    expect(rail).not.toHaveTextContent(/scoped to this fixture/i);
  });

  it('EDGE + AGENTS columns are honest "—" in LIVE (mock OFF) — no fabricated edge / per-market counts', () => {
    render(<MarketsScreen />); // mock OFF (default) → live view
    const fam = screen.getByTestId('families');
    expect(within(fam).getAllByText(/EDGE/i).length).toBeGreaterThanOrEqual(1);   // header kept for layout
    expect(within(fam).getAllByText(/AGENTS/i).length).toBeGreaterThanOrEqual(1);
    // every EDGE/AGENTS cell is the em-dash in live, NEVER a number.
    const edge = within(fam).getAllByTestId('edge-cell');
    const agents = within(fam).getAllByTestId('agents-cell');
    expect(edge.length).toBeGreaterThan(0);
    expect(edge.every((c) => c.textContent === '—')).toBe(true);
    expect(edge.some((c) => /\d/.test(c.textContent ?? ''))).toBe(false);
    expect(agents.every((c) => c.textContent === '—')).toBe(true);
    expect(agents.some((c) => /\d/.test(c.textContent ?? ''))).toBe(false);
  });

  it('under MOCK: AGENTS shows a demo count, but EDGE STAYS "—" (executable edge belongs on the Inspector)', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<MarketsScreen />);
    const fam = screen.getByTestId('families');
    const agents = within(fam).getAllByTestId('agents-cell');
    const edge = within(fam).getAllByTestId('edge-cell');
    // AGENTS (roadmappable) populates with a demo count under the DEMO banner — never "—" here.
    expect(agents.length).toBeGreaterThan(0);
    expect(agents.every((c) => /\d/.test(c.textContent ?? ''))).toBe(true);
    expect(agents.some((c) => c.textContent === '—')).toBe(false);
    // EDGE stays "—" EVEN under mock — doctrine: executable edge is a per-decision Inspector quantity.
    expect(edge.every((c) => c.textContent === '—')).toBe(true);
    expect(edge.some((c) => /\d/.test(c.textContent ?? ''))).toBe(false);
  });

  it('the disabled 1X2-HT tab states WHY it is unavailable (reuse the disabledReason idiom — not "broken")', () => {
    render(<MarketsScreen />);
    const ht = screen.getByTestId('tab-1x2-ht');
    expect(ht).toBeDisabled();
    // a user-facing reason (title + accessible name), not just a code comment, so it reads
    // "not available" rather than "broken". A regression that disables it silently fails here.
    expect(ht).toHaveAttribute('title', expect.stringMatching(/not in.*feed/i));
    expect(ht).toHaveAccessibleName(/not in.*feed/i);
  });

  it('market-type tabs filter the families; the 1X2-HT tab is honestly disabled (not in feed)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen />);
    // HT half-time market is NOT in the feed → its tab exists for layout but is disabled, not faked.
    expect(screen.getByTestId('tab-1x2-ht')).toBeDisabled();
    // clicking O/U narrows to just the Over/Under family.
    await user.click(screen.getByTestId('tab-ou'));
    const fam = screen.getByTestId('families');
    expect(within(fam).getByText(/Over \/ Under/i)).toBeInTheDocument();
    expect(within(fam).queryByText(/Match Result/i)).toBeNull();
  });

  it('LAUNCH COMPETITION pre-scopes the create flow to the selected fixture', () => {
    render(<MarketsScreen />);
    expect(screen.getByTestId('launch-competition')).toHaveAttribute('href', '/competitions/create?fixture=18172280');
  });
});
