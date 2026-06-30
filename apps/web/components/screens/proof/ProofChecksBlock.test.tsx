import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ProofChecksBlock } from '@/components/screens/proof/ProofChecksBlock';
import { sampleChecks } from '@/__tests__/fixtures/contracts';

describe('ProofChecksBlock (SEC-001/002 / AC-001/002)', () => {
  it('renders exactly the 7 trust checks with the Score Recomputed label', () => {
    render(<ProofChecksBlock checks={sampleChecks} />);
    const block = screen.getByLabelText('Proof Checks');
    expect(within(block).getAllByRole('listitem')).toHaveLength(7);
    expect(within(block).getByText('Score Recomputed')).toBeInTheDocument(); // METRICS_RECOMPUTED label
    expect(within(block).getByText('Evidence Integrity')).toBeInTheDocument();
  });

  it('binds the ANCHOR row to its real pending status (never hardcoded PASS — AC-002)', () => {
    render(<ProofChecksBlock checks={sampleChecks} />);
    const block = screen.getByLabelText('Proof Checks');
    // the pending check renders a ProofCheckChip with aria-label="pending"
    expect(within(block).getByLabelText('pending')).toBeTruthy();
  });

  it('contains NO CLV / metric in the checks block (SEC-001/AC-001)', () => {
    render(<ProofChecksBlock checks={sampleChecks} />);
    const block = screen.getByLabelText('Proof Checks');
    expect(block.textContent?.toLowerCase()).not.toContain('clv');
    expect(block.textContent?.toLowerCase()).not.toContain('sim pnl');
  });
});
