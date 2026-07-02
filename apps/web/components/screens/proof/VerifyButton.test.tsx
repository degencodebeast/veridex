import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VerifyButton } from '@/components/screens/proof/VerifyButton';
import { sampleVerifyResult } from '@/__tests__/fixtures/contracts';

const verifyProof = vi.fn();
vi.mock('@/lib/api', () => ({ verifyProof: (id: string) => verifyProof(id) }));

beforeEach(() => {
  verifyProof.mockReset();
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('VerifyButton (WD-1 / AC-020 / CON-003)', () => {
  it('calls the authoritative backend verify endpoint and renders the returned recompute + hash confirmation', async () => {
    verifyProof.mockResolvedValue(sampleVerifyResult);
    const user = userEvent.setup();
    render(<VerifyButton runId="run_7f3a" />);
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    expect(verifyProof).toHaveBeenCalledWith('run_7f3a');
    expect(await screen.findByText(/evidence hash confirmed/i)).toBeInTheDocument();
    expect(screen.getByText(/\+18\.0 bps/)).toBeInTheDocument(); // returned clv_bps
    expect(screen.getByRole('link', { name: /Open Solana tx/i })).toHaveAttribute('href', expect.stringContaining('explorer.solana.com'));
  });

  it('announces the verdict in an aria-live region (a11y)', async () => {
    verifyProof.mockResolvedValue(sampleVerifyResult);
    const user = userEvent.setup();
    render(<VerifyButton runId="run_7f3a" />);
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    const live = await screen.findByRole('status');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveTextContent('✓ Verified'); // verdict is inside the live region
  });

  it('shows ✓ Verified when verified AND no blocking check failed', async () => {
    verifyProof.mockResolvedValue(sampleVerifyResult); // verified, all blocking checks pass
    const user = userEvent.setup();
    render(<VerifyButton runId="run_7f3a" />);
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    expect(await screen.findByText('✓ Verified')).toBeInTheDocument();
  });

  it('does NOT show ✓ Verified when verified=true but a blocking check FAILED (D1 carry)', async () => {
    // The wire `verified` is evidence-hash-only; a tampered score leaves verified=true
    // but metrics_recomputed=fail. The headline must surface NOT fully verified.
    verifyProof.mockResolvedValue({
      ...sampleVerifyResult,
      verified: true,
      checks: sampleVerifyResult.checks.map((c) => (c.id === 'metrics_recomputed' ? { ...c, result: 'fail' as const } : c)),
    });
    const user = userEvent.setup();
    render(<VerifyButton runId="run_7f3a" />);
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    expect(await screen.findByText(/not fully verified/i)).toBeInTheDocument();
    expect(screen.queryByText('✓ Verified')).toBeNull(); // must NOT claim verified
    // the per-check failure is surfaced, not just the boolean
    expect(screen.getByText(/Score Recomputed/i)).toBeInTheDocument();
  });

  it('shows an honest error state if verification fails', async () => {
    verifyProof.mockRejectedValue(new Error('500'));
    const user = userEvent.setup();
    render(<VerifyButton runId="run_7f3a" />);
    await user.click(screen.getByRole('button', { name: /Verify/i }));
    expect(await screen.findByText(/verification failed/i)).toBeInTheDocument();
  });
});
