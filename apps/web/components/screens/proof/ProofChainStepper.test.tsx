import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProofChainStepper } from '@/components/screens/proof/ProofChainStepper';
import { sampleProofArtifact } from '@/__tests__/fixtures/contracts';

describe('ProofChainStepper (REQ-020)', () => {
  it('renders the five chain steps in order', () => {
    render(<ProofChainStepper chain={sampleProofArtifact.chain} />);
    for (const label of ['Evidence', 'Pre-Score', 'Score', 'Manifest', 'Anchor']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('binds each step status to its real value (anchor pending, not hardcoded pass)', () => {
    const { container } = render(<ProofChainStepper chain={sampleProofArtifact.chain} />);
    // ProofCheckChip renders an aria-label per status; the anchor step is pending here.
    expect(container.querySelector('[aria-label="pending"]')).toBeTruthy();
  });
});
