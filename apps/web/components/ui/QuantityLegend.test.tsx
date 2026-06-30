import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QuantityLegend } from '@/components/ui/QuantityLegend';

describe('QuantityLegend', () => {
  it('renders the four labeled quantities and the Stable Price caption', () => {
    render(<QuantityLegend />);
    for (const label of ['Fair Value', 'Executable Edge', 'CLV', 'Stake · Kelly']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText(/not a guaranteed true probability/i)).toBeInTheDocument();
  });
});
