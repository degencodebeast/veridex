import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { Num } from '@/components/ui/Num';

describe('Num (GUD-001 / REQ-006)', () => {
  // Load-bearing invariant: numeric color is the SIGN of the value, never decoration.
  // (Text uses the reused C1 fmtBps, which carries the " bps" unit.)
  it('renders a positive bps value with the positive class and a + sign', () => {
    const { container } = render(<Num value={18.4} kind="bps" />);
    expect(container.firstChild).toHaveClass('pos');
    // Stable, non-global selector for sign→color (raw passthrough classes were dropped).
    expect(container.firstChild).toHaveAttribute('data-sign', 'pos');
    expect(container.textContent).toBe('+18.4 bps');
  });
  it('renders a negative value with the negative class', () => {
    const { container } = render(<Num value={-3.1} kind="bps" />);
    expect(container.firstChild).toHaveClass('neg');
    expect(container.textContent).toBe('-3.1 bps');
  });
  it('renders zero with the zero class', () => {
    const { container } = render(<Num value={0} kind="bps" />);
    expect(container.firstChild).toHaveClass('zero');
  });
});
