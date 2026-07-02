import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AnchorPanel } from '@/components/screens/proof/AnchorPanel';
import { sampleProofArtifact } from '@/__tests__/fixtures/contracts';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('AnchorPanel (REQ-020 / SEC-008)', () => {
  it('shows pending honestly with no fake explorer link when not yet anchored', () => {
    render(<AnchorPanel anchor={sampleProofArtifact.anchor} />);
    expect(screen.getByText(/solana-devnet/)).toBeInTheDocument();
    expect(screen.getByText(/5-min intervals/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View on Explorer/i })).toBeNull();
  });

  it('shows the tx + explorer link once anchored', () => {
    render(<AnchorPanel anchor={{
      status: 'anchored', tx_signature: '5xQabc', cluster: 'solana-devnet', slot: 1234,
      committed_at: 1719663800, batching_note: 'odds/scores batched on 5-min intervals',
      explorer_url: 'https://explorer.solana.com/tx/5xQabc?cluster=devnet',
    }} />);
    expect(screen.getByText(/5xQabc/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View on Explorer/i })).toHaveAttribute('href', expect.stringContaining('explorer.solana.com'));
  });

  it('shows the manifest hash from verify (honest "verify to reveal" before)', () => {
    const { rerender } = render(<AnchorPanel anchor={sampleProofArtifact.anchor} />);
    expect(screen.getByText(/verify to reveal/i)).toBeInTheDocument(); // no manifest_hash yet
    rerender(<AnchorPanel anchor={{ ...sampleProofArtifact.anchor, manifest_hash: '0xMANIFESThash9931' }} />);
    expect(screen.queryByText(/verify to reveal/i)).toBeNull();
    expect(screen.getByText(/0xMANI/)).toBeInTheDocument(); // shortHash of the manifest
  });

  it('labels a not_applicable anchor as neutral n/a, NOT "Not Anchored" (offline replay)', () => {
    const { container } = render(<AnchorPanel anchor={{
      status: 'not_applicable', tx_signature: null, cluster: 'solana-devnet', slot: null,
      committed_at: null, batching_note: 'offline replay — no anchor', explorer_url: null,
    }} />);
    expect(screen.getByText('n/a')).toBeInTheDocument();
    expect(container.textContent?.toLowerCase()).not.toContain('not anchored');
  });
});
