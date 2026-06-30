import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CockpitScreen } from '@/components/screens/cockpit/CockpitScreen';
import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { ProofCardScreen } from '@/components/screens/proof/ProofCardScreen';
import { inspectorHref, proofHref } from '@/lib/deeplinks';
import { sampleCockpitState, sampleInspectorRecord, sampleProofArtifact } from '@/__tests__/fixtures/contracts';

vi.mock('next/navigation', () => ({ usePathname: () => '/arena/wc-fra-bra' }));
vi.mock('@/hooks/useArenaStream', () => ({
  useArenaStream: () => ({ state: sampleCockpitState, wsStatus: 'connected' }),
}));
vi.mock('@/lib/api', () => ({ verifyProof: vi.fn() }));

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('killer flow (REQ-004 / AC-021)', () => {
  it('cockpit AGENT_ACTION row links to the Inspector for that action', () => {
    render(<CockpitScreen competitionId="wc-fra-bra" initial={sampleCockpitState} />);
    // Derive the seq from the fixture's AGENT_ACTION event, not a hardcoded literal,
    // so the link contract is asserted against the real data the cockpit renders.
    const agentAction = sampleCockpitState.events.find((e) => e.type === 'AGENT_ACTION');
    expect(agentAction).toBeDefined();
    const expected = inspectorHref(sampleCockpitState.run_id, agentAction!.seq);
    expect(screen.getByRole('link', { name: /AGENT_ACTION/i })).toHaveAttribute('href', expected);
  });

  it('Inspector links forward to the full Proof Card for the same run', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByRole('link', { name: /View Full Proof Card/i }))
      .toHaveAttribute('href', proofHref(sampleInspectorRecord.run_id));
  });

  it('Proof Card terminates the flow with the trust separation intact (AC-001)', () => {
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    const checks = screen.getByLabelText('Proof Checks');
    expect(checks.textContent?.toLowerCase()).not.toContain('clv');
    expect(screen.getByLabelText('Performance Metrics')).toBeInTheDocument();
  });
});
