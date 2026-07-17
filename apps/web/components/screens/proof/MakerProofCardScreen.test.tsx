import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, within } from '@testing-library/react';
import { MakerProofCardScreen } from '@/components/screens/proof/MakerProofCardScreen';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
import { MAKER_INCONCLUSIVE, MAKER_INVERTED } from '../../../__tests__/fixtures/makerVariants';

describe('MakerProofCardScreen (Maker Arena MM-R1)', () => {
  it('leads with the falsification verdict + CI, not the mean', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(within(screen.getByTestId('maker-proof-verdict')).getByText(/separated/i)).toBeInTheDocument();
    expect(screen.getByTestId('maker-proof-delta')).toHaveTextContent(/\+43/);
    expect(screen.getByTestId('maker-proof-ci')).toHaveTextContent('[34, 52]');
  });

  it('always shows the n=18 small-sample caveat', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(screen.getAllByText(/n=18/i).length).toBeGreaterThan(0);
    expect(within(screen.getByTestId('maker-proof-universe')).getByText('18')).toBeInTheDocument();
  });

  it('no fill / PnL / executable-edge claim — real_executable_edge_bps is null by construction', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(within(screen.getByTestId('maker-proof-edge-caveat')).getByText(/null by construction/i)).toBeInTheDocument();
  });

  it('MM-R1 only — future rungs render honest-empty, never a fabricated R1.5/R2 value', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    const rungs = screen.getAllByTestId('maker-proof-empty-rung');
    expect(rungs.length).toBe(3);
    rungs.forEach((r) => expect(r.textContent).toMatch(/future|not present at MM-R1|not yet surfaced/i));
  });

  it('deep-links back to the maker leaderboard lane', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    expect(screen.getByRole('link', { name: /maker leaderboard/i })).toHaveAttribute('href', '/leaderboard?lane=maker');
  });

  it('SEC-005: never imports/reuses the directional CLV ProofArtifact type', () => {
    const src = readFileSync(resolve(__dirname, './MakerProofCardScreen.tsx'), 'utf8');
    const importLines = src.split('\n').filter((l) => l.trim().startsWith('import'));
    expect(importLines.some((l) => /\bProofArtifact\b/.test(l))).toBe(false);
    expect(importLines.some((l) => /\badaptProofArtifact\b/.test(l))).toBe(false);
  });
});

// I-R remediation (M1 / M3): the lead claim must derive from the REAL verdict, and the sealed
// configuration identity (config_hash) must stay visible on the card header.
describe('MakerProofCardScreen — verdict honesty + configuration identity (I-R M1, M3)', () => {
  it('M1: INCONCLUSIVE leads with the inconclusive claim — no separated/CI-above-zero language', () => {
    render(<MakerProofCardScreen result={MAKER_INCONCLUSIVE} agentId="txline-fair-mm" />);
    expect(within(screen.getByTestId('maker-proof-verdict')).getByText('Inconclusive')).toBeInTheDocument();
    expect(screen.queryByText(/txline-fair separated|separated from the naive control/i)).toBeNull();
    expect(screen.queryByText(/whole ci above zero/i)).toBeNull();
    expect(screen.getByText(/no separation/i)).toBeInTheDocument();
  });

  it('M1: INVERTED leads with the inverted claim — the candidate is never called separated or less toxic', () => {
    render(<MakerProofCardScreen result={MAKER_INVERTED} agentId="txline-fair-mm" />);
    expect(within(screen.getByTestId('maker-proof-verdict')).getByText('Inverted')).toBeInTheDocument();
    expect(screen.queryByText(/txline-fair separated|separated from the naive control/i)).toBeNull();
    expect(screen.queryByText(/whole ci above zero/i)).toBeNull();
    expect(screen.getByText(/reliably more toxic|reliably worse/i)).toBeInTheDocument();
  });

  it('M3: the exact sealed config_hash renders in the header (short-hash text, full hash visible)', () => {
    render(<MakerProofCardScreen result={MAKER_ARENA_RESULT} agentId="txline-fair-mm" />);
    const full = 'f997d5a8fcb7d7c4cb02048a56bfb7bcdfabc06c6657ea97bf84be43beb16f33';
    const el = screen.getByTestId('maker-proof-config-hash');
    expect(el).toHaveTextContent('f997d5…6f33'); // shortHash() convention
    expect(el).toHaveAttribute('title', full); // the EXACT result configuration stays recoverable
  });
});
