import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProofTraceStrip } from '@/components/screens/cockpit/ProofTraceStrip';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';

describe('ProofTraceStrip (REQ-011)', () => {
  it('renders all six trace stages in order', () => {
    render(<ProofTraceStrip trace={sampleCockpitState.trace} />);
    for (const label of ['Evidence', 'Law', 'Policy', 'Receipt', 'Score', 'Anchor']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('labels itself a projection of the canonical log, not a source of truth', () => {
    render(<ProofTraceStrip trace={sampleCockpitState.trace} />);
    expect(screen.getByText(/projection of the canonical log/i)).toBeInTheDocument();
  });
});
