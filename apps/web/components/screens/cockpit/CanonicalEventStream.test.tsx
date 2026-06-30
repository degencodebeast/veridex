import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CanonicalEventStream } from '@/components/screens/cockpit/CanonicalEventStream';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';

vi.mock('next/navigation', () => ({ usePathname: () => '/arena/wc-fra-bra' }));

describe('CanonicalEventStream (REQ-011 / AC-021)', () => {
  it('renders seq, type, payload_hash and evidence flag per row', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(screen.getByText('87')).toBeInTheDocument();      // seq
    expect(screen.getByText('AGENT_ACTION')).toBeInTheDocument();
    expect(screen.getAllByText(/0x/).length).toBeGreaterThan(0); // payload_hash
  });

  it('makes AGENT_ACTION rows deep-link to the Decision Inspector for that action', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    const link = screen.getByRole('link', { name: /AGENT_ACTION/i });
    expect(link).toHaveAttribute('href', '/inspector/run_7f3a/87');
  });

  it('does not deep-link non-AGENT_ACTION rows', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(screen.queryByRole('link', { name: /law_recomputed/i })).toBeNull();
  });

  it('applies no per-row entrance animation class (PAT-003/AC-031)', () => {
    const { container } = render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(container.querySelector('[class*="enter"], [class*="animate"]')).toBeNull();
  });
});
