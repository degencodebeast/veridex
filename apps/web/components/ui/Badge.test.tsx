import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Badge } from '@/components/ui/Badge';
import { BADGE_VARIANTS } from '@/lib/badges';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('Badge (PAT-002: one component, full vocabulary)', () => {
  it('covers the full 13-variant status vocabulary plus the Maker Arena (MM-R1) + QuoteGuard ablation additions', () => {
    expect([...BADGE_VARIANTS].sort()).toEqual(
      [
        'anchored', 'eligible', 'invalid', 'live', 'llm', 'not-anchored',
        'not-eligible', 'partial', 'pending', 'replay', 'reproducible',
        'valid', 'verified',
        // Maker Arena lane (MM-R1) — falsification verdicts + rung/caveat chips.
        'mm-r1', 'separated', 'inconclusive', 'inverted', 'uncalibrated', 'small-n', 'trades-not-fills',
        // QuoteGuard behavior ablation (F-8) — behavior-comparison labels, never a rank/winner.
        'behavior-ablation', 'not-a-leaderboard', 'recorded-replay', 'same-strategy-tape',
        'diverges-true', 'diverges-false', 'guard-on', 'guard-off',
        // AC-30/AC-31 — third-party print / counterfactual capacity ceiling, never a fill/PnL/rank.
        'counterfactual',
      ].sort(),
    );
  });

  it('renders the default label for a variant', () => {
    render(<Badge variant="reproducible" />);
    expect(screen.getByText(/reproducible/i)).toBeInTheDocument();
  });

  it('applies the variant class for token-driven styling', () => {
    const { container } = render(<Badge variant="valid" />);
    expect(container.firstChild).toHaveClass('valid');
  });

  it('embeds a pulsing LiveDot for the live variant', () => {
    const { container } = render(<Badge variant="live" />);
    expect(container.querySelector('span[data-livedot]')).toBeTruthy();
  });

  it('renders without crashing for every variant', () => {
    for (const v of BADGE_VARIANTS) {
      const { container } = render(<Badge variant={v} />);
      expect(container.firstChild).toBeTruthy();
    }
  });
});
