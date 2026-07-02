import { describe, it, expect } from 'vitest';
import { deriveProofChain } from '@/lib/proof';
import { sampleProofArtifact } from '@/__tests__/fixtures/contracts';

describe('deriveProofChain (REQ-020: chain status DERIVED from the 7 checks)', () => {
  it('maps each step status from its corresponding check — a failing check fails its step', () => {
    const failing = {
      ...sampleProofArtifact,
      checks: sampleProofArtifact.checks.map((c) => (c.id === 'metrics_recomputed' ? { ...c, result: 'fail' as const } : c)),
    };
    const chain = deriveProofChain(failing, '');
    expect(chain.find((s) => s.id === 'score')?.status).toBe('fail');      // metrics_recomputed → score
    expect(chain.find((s) => s.id === 'evidence')?.status).toBe('pass');   // evidence_integrity still passes
    expect(chain.find((s) => s.id === 'anchor')?.status).toBe('pending');  // anchor check is pending
  });

  it('shows the evidence hash it has, and the manifest hash only after verify (honest "—" before)', () => {
    const before = deriveProofChain(sampleProofArtifact, '');
    expect(before.find((s) => s.id === 'evidence')?.hash).toBe(sampleProofArtifact.evidence_hash);
    expect(before.find((s) => s.id === 'manifest')?.hash).toBe('—'); // not revealed until verify
    expect(before.find((s) => s.id === 'pre-score')?.hash).toBe('—'); // intermediate hash not in wire

    const after = deriveProofChain(sampleProofArtifact, '0xMANIFEST');
    expect(after.find((s) => s.id === 'manifest')?.hash).toBe('0xMANIFEST');
  });

  it('emits exactly the six chain steps in order (policy/exec is the 6th)', () => {
    expect(deriveProofChain(sampleProofArtifact, '').map((s) => s.id)).toEqual(
      ['evidence', 'pre-score', 'policy', 'score', 'manifest', 'anchor'],
    );
  });

  it('derives the policy/exec step from policy_obeyed + receipt_separation — not_applicable when both n/a', () => {
    // both executor-lane checks pass (fixture) → policy step passes; hash honestly '—' (roots panel carries the forest)
    const passing = deriveProofChain(sampleProofArtifact, '');
    expect(passing.find((s) => s.id === 'policy')?.status).toBe('pass');
    expect(passing.find((s) => s.id === 'policy')?.hash).toBe('—');

    // Plan-A replay has no executor lane → both checks not_applicable → honest not_applicable (no false-green)
    const noLane = {
      ...sampleProofArtifact,
      checks: sampleProofArtifact.checks.map((c) =>
        c.id === 'policy_obeyed' || c.id === 'receipt_separation' ? { ...c, result: 'not_applicable' as const } : c),
    };
    expect(deriveProofChain(noLane, '').find((s) => s.id === 'policy')?.status).toBe('not_applicable');

    // a blocking policy_obeyed FAIL dominates → step fails
    const failed = {
      ...sampleProofArtifact,
      checks: sampleProofArtifact.checks.map((c) =>
        c.id === 'policy_obeyed' ? { ...c, result: 'fail' as const } : c),
    };
    expect(deriveProofChain(failed, '').find((s) => s.id === 'policy')?.status).toBe('fail');
  });
});
