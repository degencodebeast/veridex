import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ExecutionLane } from '@/components/screens/cockpit/ExecutionLane';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';

describe('ExecutionLane (REQ-011 / SEC-004)', () => {
  it('renders the lifecycle stages', () => {
    render(<ExecutionLane receipts={sampleCockpitState.receipts} />);
    for (const s of ['proposed', 'law_approved', 'policy_approved', 'submitted', 'filled']) {
      expect(screen.getByText(new RegExp(s.replace('_', ' '), 'i'))).toBeInTheDocument();
    }
  });

  it('labels the receipt a non-scoring off-chain venue artifact (SEC-004)', () => {
    render(<ExecutionLane receipts={sampleCockpitState.receipts} />);
    expect(screen.getByText(/off-chain venue artifact/i)).toBeInTheDocument();
    expect(screen.getByText(/non-scoring/i)).toBeInTheDocument();
  });
});
