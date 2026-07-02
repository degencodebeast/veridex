import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConfBar } from '@/components/ui/ConfBar';

describe('ConfBar (WD-7)', () => {
  it('shows HIGH confidence for a large sample and no low-sample flag', () => {
    render(<ConfBar validCount={114} />);
    expect(screen.getByText(/CONF/)).toHaveTextContent(/HIGH/);
    expect(screen.queryByText(/low sample/i)).toBeNull();
  });
  it('flags a low sample but still shows the row (never hidden)', () => {
    render(<ConfBar validCount={6} />);
    expect(screen.getByText(/CONF/)).toHaveTextContent(/LOW/);
    expect(screen.getByText(/low sample/i)).toBeInTheDocument();
  });
});
