import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ArenaEmptyState } from '@/components/screens/ArenaEmptyState';

describe('ArenaEmptyState (REQ-005 / AC-022)', () => {
  it('shows a clean no-live state linking upcoming competitions + settled proofs', () => {
    render(<ArenaEmptyState />);
    expect(screen.getByText(/no live competition/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /competitions/i })).toHaveAttribute('href', '/competitions');
    expect(screen.getByRole('link', { name: /markets/i })).toHaveAttribute('href', '/markets');
  });
});
