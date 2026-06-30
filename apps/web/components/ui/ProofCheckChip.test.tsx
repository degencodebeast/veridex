import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ProofCheckChip } from '@/components/ui/ProofCheckChip';

describe('ProofCheckChip (PAT-002: 24px proof-check status mark)', () => {
  it('shows a check glyph for pass', () => {
    const { container } = render(<ProofCheckChip status="pass" />);
    expect(container.textContent).toContain('✓');
    expect(container.firstChild).toHaveClass('pass');
  });

  it('shows a bang glyph for fail', () => {
    const { container } = render(<ProofCheckChip status="fail" />);
    expect(container.textContent).toContain('!');
    expect(container.firstChild).toHaveClass('fail');
  });

  it('shows a bang glyph for pending', () => {
    const { container } = render(<ProofCheckChip status="pending" />);
    expect(container.textContent).toContain('!');
    expect(container.firstChild).toHaveClass('pending');
  });

  it('shows a circle glyph for not_applicable', () => {
    const { container } = render(<ProofCheckChip status="not_applicable" />);
    expect(container.textContent).toContain('○');
    expect(container.firstChild).toHaveClass('notApplicable');
  });

  // Trust property: a non-pass status must NEVER render the pass glyph (no
  // hardcoded-PASS path on a proof-UI primitive). The type guards bad calls;
  // this locks the runtime guarantee.
  it.each(['fail', 'pending', 'not_applicable'] as const)(
    'never renders the pass glyph for %s',
    (status) => {
      const { container } = render(<ProofCheckChip status={status} />);
      expect(container.textContent).not.toContain('✓');
    },
  );
});
