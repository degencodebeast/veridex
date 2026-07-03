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

  // ---- Edge DISPLAY GATE (REQ-2D-501) on the cockpit's policy stream ----

  it('DISPLAY GATE: renders the edge VALUE only for a real venue quote; a Fake/paper quote shows only the min THRESHOLD', () => {
    const { container } = render(<PolicyDecisions decisions={[
      // real quote → the executable edge value legibly renders alongside the min threshold
      { tick_seq: 1, decision: 'ALLOW', reason: 'edge ≥ min', edge_bps: 22, min_edge_bps: 8, real_venue_quote: true },
      // Fake/paper quote → the 999 edge number must NEVER surface as edge; the config min still shows
      { tick_seq: 2, decision: 'ALLOW', reason: 'edge ≥ min', edge_bps: 999, min_edge_bps: 8, real_venue_quote: false },
    ]} killArmed={false} />);
    const rows = Array.from(container.querySelectorAll('li'));
    expect(rows[0].textContent).toContain('edge 22'); // real quote → edge value renders
    expect(rows[1].textContent).not.toContain('999');  // Fake quote → edge value gated out
    expect(rows[1].textContent).toContain('min 8 bps'); // the config threshold is honest, always shown
  });

  it('DISPLAY GATE: no edge digit renders when the real-quote flag is absent (fail-closed)', () => {
    const { container } = render(<PolicyDecisions decisions={[
      { tick_seq: 1, decision: 'ALLOW', reason: 'edge ≥ min', edge_bps: 42, min_edge_bps: 8 },
    ]} killArmed={false} />);
    expect(container.textContent).not.toContain('edge 42');
  });
});
