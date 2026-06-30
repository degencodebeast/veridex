import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PolicyDecisions } from '@/components/screens/cockpit/PolicyDecisions';

describe('PolicyDecisions (REQ-011)', () => {
  it('renders ALLOW/DENY/REFUSE decisions with edge context', () => {
    render(<PolicyDecisions decisions={[
      { tick_seq: 1, decision: 'ALLOW', reason: 'edge ≥ min', edge_bps: 22, min_edge_bps: 8 },
      { tick_seq: 2, decision: 'DENY', reason: 'edge < min', edge_bps: 4, min_edge_bps: 8 },
    ]} killArmed={false} />);
    expect(screen.getByText('ALLOW')).toBeInTheDocument();
    expect(screen.getByText('DENY')).toBeInTheDocument();
  });

  it('shows KILL ARMED only when armed', () => {
    const { rerender } = render(<PolicyDecisions decisions={[]} killArmed={false} />);
    expect(screen.queryByText(/KILL ARMED/i)).toBeNull();
    rerender(<PolicyDecisions decisions={[]} killArmed />);
    expect(screen.getByText(/KILL ARMED/i)).toBeInTheDocument();
  });

  it('exposes no editable affordances (read-only in cockpit; controls live in Ops drawer)', () => {
    const { container } = render(<PolicyDecisions decisions={[]} killArmed />);
    expect(container.querySelectorAll('input, textarea, select, button').length).toBe(0);
  });
});
