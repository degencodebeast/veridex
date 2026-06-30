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

  it('emits exactly the five chain steps in order', () => {
    expect(deriveProofChain(sampleProofArtifact, '').map((s) => s.id)).toEqual(
      ['evidence', 'pre-score', 'score', 'manifest', 'anchor'],
    );
  });
});
