import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { OnChainValidationBlock } from '@/components/screens/proof/OnChainValidationBlock';
import { sampleProofArtifact } from '@/__tests__/fixtures/contracts';

describe('OnChainValidationBlock (REQ-043 / AC-013)', () => {
  it('labels each validation by its method against the txoracle root', () => {
    render(<OnChainValidationBlock validations={sampleProofArtifact.validations} />);
    const block = screen.getByLabelText('On-Chain Validation');
    expect(within(block).getByText('validateOdds')).toBeInTheDocument();
    expect(within(block).getByText('validateFixtureBatch')).toBeInTheDocument();
    expect(within(block).getByText('validateStat')).toBeInTheDocument();
  });

  it('never relabels odds as a stat validation (honesty guard — AC-013)', () => {
    render(<OnChainValidationBlock validations={[
      { method: 'validateStat', data_kind: 'odds', message_id: 'm1', result: 'pass', root: '0xr' },
    ]} />);
    expect(screen.getByText(/label mismatch/i)).toBeInTheDocument();
  });

  it('renders an honest empty state when no per-entry validations are in the artifact (gap)', () => {
    render(<OnChainValidationBlock validations={[]} />);
    const block = screen.getByLabelText('On-Chain Validation');
    // never fabricate a validations[] list; state the honest absence (Phase 2D).
    expect(within(block).getByText(/not surfaced in this proof artifact/i)).toBeInTheDocument();
    expect(within(block).getByText(/computed at ingest/i)).toBeInTheDocument();
  });
});
