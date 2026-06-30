import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ExecutionLane } from '@/components/screens/cockpit/ExecutionLane';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { ExecutionReceipt, ReceiptStatus } from '@/lib/contracts';

function mkReceipt(over: Partial<ExecutionReceipt> & { status: ReceiptStatus }): ExecutionReceipt {
  return {
    execution_id: 'ex', venue: 'SX Bet', market_ref: '1X2:FRA', side: 'FRA',
    requested_size: 100, filled_size: 0, price: 1.472, venue_order_id: null, mode: 'paper',
    submitted_at: null, settled_at: null, ...over,
  };
}
const reachedAttr = (c: HTMLElement, stage: string) =>
  c.querySelector(`[data-stage="${stage}"]`)?.getAttribute('data-reached');

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

  it('lights stages only up to a half-progressed positive status (submitted ≠ filled)', () => {
    const { container } = render(<ExecutionLane receipts={[mkReceipt({ status: 'submitted', submitted_at: 1 })]} />);
    expect(reachedAttr(container, 'policy_approved')).toBe('true');
    expect(reachedAttr(container, 'submitted')).toBe('true');
    expect(reachedAttr(container, 'filled')).toBe('false'); // contrast: NOT all-lit
  });

  it('shows progress up to the rejection point + a rejected indicator (terminal-negative)', () => {
    // A receipt that reached submitted (submitted_at set) then was rejected.
    const { container } = render(<ExecutionLane receipts={[mkReceipt({ status: 'rejected', submitted_at: 1, settled_at: 2 })]} />);
    expect(reachedAttr(container, 'submitted')).toBe('true');  // progress preserved (not all-off)
    expect(reachedAttr(container, 'filled')).toBe('false');    // never filled
    expect(screen.getByText(/rejected/i)).toBeInTheDocument(); // clear terminal indicator
  });
});
