import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { JsonPanel } from '@/components/ui/JsonPanel';

describe('JsonPanel', () => {
  it('renders a title and the data', () => {
    render(<JsonPanel title="AgentAction" data={{ type: 'FLAG_VALUE' }} />);
    expect(screen.getByText('AgentAction')).toBeInTheDocument();
    expect(screen.getByText(/FLAG_VALUE/)).toBeInTheDocument();
  });

  it('marks the accent (trusted-output) variant', () => {
    const { container } = render(<JsonPanel title="Recompute" data={{ valid: true }} accent />);
    expect(container.firstChild).toHaveClass('accent');
  });
});
