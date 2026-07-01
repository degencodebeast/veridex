import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ProofCardScreen } from '@/components/screens/proof/ProofCardScreen';
import { sampleProofArtifact, offlineReplayProofArtifact } from '@/__tests__/fixtures/contracts';
import { PROOF_DEMO_ROOTS, EMPTY_ROOT } from '@/lib/fixtures/proof';
import { shortHash } from '@/lib/format';
import { GLOSSARY } from '@/lib/glossary';
import type { CheckResult } from '@/lib/contracts';

// Demo-accurate replay artifact: the executor-lane checks are honestly not_applicable
// (matches the served proof_artifact.json — Plan-A replay has no executor lane).
const demoReplayArtifact = {
  ...offlineReplayProofArtifact,
  checks: offlineReplayProofArtifact.checks.map((c): CheckResult =>
    c.id === 'policy_obeyed' || c.id === 'receipt_separation'
      ? { ...c, result: 'not_applicable' }
      : c),
  roots: PROOF_DEMO_ROOTS,
};

// Reach the ProofCheckChip status for a named step inside the Proof chain stepper.
function stepStatus(chain: HTMLElement, label: string): string | null {
  const step = within(chain).getByText(label).closest('div')!.parentElement!;
  return step.querySelector('[data-status]')!.getAttribute('data-status');
}

vi.mock('@/lib/api', () => ({ verifyProof: vi.fn() }));

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('ProofCardScreen (REQ-020 / SEC-001 / AC-001/002)', () => {
  it('renders the two separate blocks with CLV only in Performance Metrics', () => {
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    const checks = screen.getByLabelText('Proof Checks');
    const metrics = screen.getByLabelText('Performance Metrics');
    expect(checks.textContent?.toLowerCase()).not.toContain('clv');     // AC-001
    expect(within(metrics).getByText(/^CLV/)).toBeInTheDocument();
  });

  it('shows the 7 checks + chain + anchor + verify control', () => {
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    expect(within(screen.getByLabelText('Proof Checks')).getAllByRole('listitem')).toHaveLength(7);
    expect(screen.getByLabelText('Proof chain')).toBeInTheDocument();
    expect(screen.getByLabelText('Anchor')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Verify/i })).toBeInTheDocument();
  });

  it('renders ANCHOR as not_applicable for an offline-replay run (AC-002)', () => {
    render(<ProofCardScreen artifact={offlineReplayProofArtifact} />);
    expect(within(screen.getByLabelText('Proof Checks')).getByLabelText('not_applicable')).toBeTruthy();
  });
});

// ── DENSITY: Merkle roots · 6th policy/exec step · glossary-sourced InfoTips ──
describe('ProofCardScreen density — MERKLE ROOTS panel', () => {
  it('renders the 6 REAL named roots in PROOF_ROOT_ORDER', () => {
    render(<ProofCardScreen artifact={{ ...sampleProofArtifact, roots: PROOF_DEMO_ROOTS }} />);
    const panel = screen.getByLabelText('Merkle roots');
    ['Event Log', 'Score', 'Receipt', 'Policy', 'Competition', 'Payout Reserved']
      .forEach((l) => expect(within(panel).getByText(l)).toBeInTheDocument());
  });

  it('shows the EMPTY_ROOT honestly for the 3 no-executor-lane domains — never fake-populated (TEETH)', () => {
    render(<ProofCardScreen artifact={{ ...sampleProofArtifact, roots: PROOF_DEMO_ROOTS }} />);
    const panel = screen.getByLabelText('Merkle roots');
    // receipt / policy / payout_reserved carry sha256(b"") — marked empty, not demo hex
    expect(within(panel).getAllByText(/empty · no records/i)).toHaveLength(3);
    expect(within(panel).getAllByText(shortHash(EMPTY_ROOT)).length).toBeGreaterThanOrEqual(3);
    // the demo-populated domains do NOT carry the empty hash
    expect(within(panel).getByText('Event Log').closest('div')!.textContent)
      .not.toContain(shortHash(EMPTY_ROOT));
  });

  it('is honest-empty in LIVE (roots===[]) — a note, and NO demo hex leaks (RED-proof)', () => {
    render(<ProofCardScreen artifact={{ ...sampleProofArtifact, roots: [] }} />);
    const panel = screen.getByLabelText('Merkle roots');
    expect(within(panel).getByText(/not yet surfaced by the API/i)).toBeInTheDocument();
    // a demo root leaking into the live view would fail here
    expect(panel.textContent).not.toContain('9f2c1a'); // event_log demo hex prefix
    expect(within(panel).queryByText('Event Log')).toBeNull();
  });
});

describe('ProofCardScreen density — 6th policy/exec proof-chain step', () => {
  it('renders 6 chain steps with the policy step honest not_applicable on a demo replay (TEETH)', () => {
    render(<ProofCardScreen artifact={demoReplayArtifact} />);
    const chain = screen.getByLabelText('Proof chain');
    expect(chain.querySelectorAll('[data-status]')).toHaveLength(6);
    expect(stepStatus(chain, 'Policy')).toBe('not_applicable');
  });

  it('DERIVES the policy step from policy_obeyed + receipt_separation — not hardcoded n/a', () => {
    // sampleProofArtifact has both executor-lane checks = pass → step must reflect pass
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    const chain = screen.getByLabelText('Proof chain');
    expect(stepStatus(chain, 'Policy')).toBe('pass');
  });

  it('shows a blocking policy_obeyed FAIL as a failed step — no false-green', () => {
    const failed = {
      ...sampleProofArtifact,
      checks: sampleProofArtifact.checks.map((c): CheckResult =>
        c.id === 'policy_obeyed' ? { ...c, result: 'fail' } : c),
    };
    render(<ProofCardScreen artifact={failed} />);
    expect(stepStatus(screen.getByLabelText('Proof chain'), 'Policy')).toBe('fail');
  });
});

describe('ProofCardScreen density — glossary-sourced InfoTips (drift-guard)', () => {
  it('InfoTip copy is single-sourced from lib/glossary.ts — verbatim, no per-screen drift', () => {
    render(<ProofCardScreen artifact={{ ...sampleProofArtifact, roots: PROOF_DEMO_ROOTS }} />);
    expect(screen.getByText(GLOSSARY.checks_vs_metrics.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.verifier_version.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.proof_mode.definition)).toBeInTheDocument();
    // the manifest-hash / roots commitment jargon reuses the canonical anchor entry (no drift)
    expect(screen.getAllByText(GLOSSARY.anchor.definition).length).toBeGreaterThanOrEqual(1);
  });
});
