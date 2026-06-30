import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ProofCardScreen } from '@/components/screens/proof/ProofCardScreen';
import { sampleProofArtifact, offlineReplayProofArtifact } from '@/__tests__/fixtures/contracts';

vi.mock('@/lib/api', () => ({ verifyProof: vi.fn() }));

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('ProofCardScreen (REQ-020 / SEC-001 / AC-001/002)', () => {
  it('renders the two separate blocks with CLV only in Performance Metrics', () => {
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    const checks = screen.getByLabelText('Proof Checks');
    const metrics = screen.getByLabelText('Performance Metrics');
    expect(checks.textContent?.toLowerCase()).not.toContain('clv');     // AC-001
    expect(within(metrics).getByText(/^CLV/)).toBeInTheDocument();
  });

  it('shows the 7 checks + chain + anchor + verify control', () => {
    render(<ProofCardScreen artifact={sampleProofArtifact} />);
    expect(within(screen.getByLabelText('Proof Checks')).getAllByRole('listitem')).toHaveLength(7);
    expect(screen.getByLabelText('Proof chain')).toBeInTheDocument();
    expect(screen.getByLabelText('Anchor')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Verify/i })).toBeInTheDocument();
  });

  it('renders ANCHOR as not_applicable for an offline-replay run (AC-002)', () => {
    render(<ProofCardScreen artifact={offlineReplayProofArtifact} />);
    expect(within(screen.getByLabelText('Proof Checks')).getByLabelText('not_applicable')).toBeTruthy();
  });
});
