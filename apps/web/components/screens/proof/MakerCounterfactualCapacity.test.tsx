import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, within } from '@testing-library/react';
import { MakerProofCardScreen } from '@/components/screens/proof/MakerProofCardScreen';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
import type { MakerArenaResultView, MakerCapacityClaim } from '@/lib/contracts';

// AC-30 / AC-31 (Fable M3) — the primary net-new maker honesty deliverable. A maker result that
// carries a third-party print / counterfactual entry-or-exit capacity MUST render as a LABELED,
// BOUNDED counterfactual — never as our own fill / PnL / rank / executable edge (Gate-B
// REQ-026/027/052/053). When no such claim is present the surface renders exactly as before.
const CF_CLAIM: MakerCapacityClaim = {
  kind: 'observed_market_print',
  capacity_usd: 5000,
  matched_observed_liquidity_usd: 1200, // capacity is bounded DOWN to this observed ceiling
};

function withCapacity(claim: MakerCapacityClaim): MakerArenaResultView {
  return { ...MAKER_ARENA_RESULT, historical_capacity: claim };
}

describe('MakerProofCardScreen — counterfactual capacity honesty (AC-30/AC-31)', () => {
  it('renders a labeled-counterfactual panel when the result carries a third-party/counterfactual capacity', () => {
    render(<MakerProofCardScreen result={withCapacity(CF_CLAIM)} agentId="txline-fair-mm" />);
    const panel = screen.getByTestId('maker-proof-counterfactual');
    expect(panel).toBeInTheDocument();
    expect(within(panel).getByText(/counterfactual/i)).toBeInTheDocument();
    // it is explicitly NOT our fill / receipt / PnL / rank
    expect(panel.textContent).toMatch(/not (our|a) fill|third-party|never a fill/i);
  });

  it('BOUNDS the displayed capacity by matched observed liquidity — the ceiling, never the wish', () => {
    render(<MakerProofCardScreen result={withCapacity(CF_CLAIM)} agentId="txline-fair-mm" />);
    const panel = screen.getByTestId('maker-proof-counterfactual');
    // the bounded ceiling (1200) is shown; the unbounded 5000 wish is never presented as capacity
    expect(panel.textContent).toMatch(/1,?200/);
    expect(panel.textContent).not.toMatch(/5,?000/);
  });

  it('never routes the counterfactual capacity into the executable-edge / fill slot (stays null by construction)', () => {
    render(<MakerProofCardScreen result={withCapacity(CF_CLAIM)} agentId="txline-fair-mm" />);
    // the exec-edge caveat is untouched: still the honest null-by-construction claim, not the capacity
    const edge = screen.getByTestId('maker-proof-edge-caveat');
    expect(within(edge).getByText(/null by construction/i)).toBeInTheDocument();
    expect(edge.textContent).not.toMatch(/1,?200|5,?000/);
  });

  it('four evidence classes stay visibly distinct — the counterfactual panel is separate from the rank/edge surfaces', () => {
    render(<MakerProofCardScreen result={withCapacity(CF_CLAIM)} agentId="txline-fair-mm" />);
    const panel = screen.getByTestId('maker-proof-counterfactual');
    // the rank axis (toxicity) and the null exec-edge each live in their OWN surface, not inside the
    // counterfactual panel — no class is nested inside another (no conflation).
    expect(within(panel).queryByTestId('maker-proof-edge-caveat')).toBeNull();
    expect(panel.getAttribute('data-evidence-class')).toBe('counterfactual');
  });

  it('renders the honest-empty rungs (no fabricated capacity) when no counterfactual claim is present', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(screen.queryByTestId('maker-proof-counterfactual')).toBeNull();
    // the honest-empty future rungs remain (backward-compatible with the sealed MM-R1 fixture)
    expect(screen.getAllByTestId('maker-proof-empty-rung').length).toBeGreaterThan(0);
  });
});

// Provenance at the maker joins (AC-23) — the Maker Proof Card is a RECORDED replay artifact, never
// live, and it carries no external-anchor / Gate-B authority claim. (The Duel not-anchored honesty
// and the GuardAblation recorded-replay/not_anchored/never-live provenance are locked in their own
// suites; this closes the remaining Proof Card join.)
describe('Maker Proof Card provenance — replay-never-live, no fabricated anchor/Gate-B authority', () => {
  it('renders the recorded-replay source mode and never a live badge', () => {
    const { container } = render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(container.textContent).toMatch(/replay/i); // sealed recorded TxLINE replay, never "live"
    expect(container.querySelector('[data-variant="live"]')).toBeNull();
  });

  it('makes no external-anchor claim on the sealed maker result (no anchored badge)', () => {
    const { container } = render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(container.querySelector('[data-variant="anchored"]')).toBeNull();
  });
});

// The counterfactual evidence class must NEVER leak into the directional / CLV lane. The directional
// render + adapter paths stay byte-uncontaminated by the maker honesty layer (RED: directional lane
// byte-unchanged).
describe('directional lane byte-unchanged — no counterfactual/evidence-class leak (AC-30 boundary)', () => {
  const read = (rel: string) => readFileSync(resolve(__dirname, rel), 'utf8');

  it('the directional Proof Card carries no counterfactual/historical-capacity artifact', () => {
    const src = read('./ProofCardScreen.tsx');
    expect(/counterfactual/i.test(src)).toBe(false);
    expect(/historical_capacity/i.test(src)).toBe(false);
  });

  it('the shared leaderboard adapter never adapts a maker counterfactual into the directional row', () => {
    const api = read('../../../lib/api.ts');
    // adaptLeaderboard (the directional/CLV adapter) must not reference the maker capacity claim
    const adapterSlice = api.slice(api.indexOf('export function adaptLeaderboard'), api.indexOf('export function adaptMakerArenaResult'));
    expect(/counterfactual|historical_capacity/i.test(adapterSlice)).toBe(false);
  });
});
