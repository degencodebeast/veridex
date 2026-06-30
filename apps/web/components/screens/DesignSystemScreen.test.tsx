import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { DesignSystemScreen } from '@/components/screens/DesignSystemScreen';
import { BADGE_VARIANTS } from '@/lib/badges';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('DesignSystemScreen (REQ-026)', () => {
  it('renders the reference sections', () => {
    render(<DesignSystemScreen />);
    expect(screen.getByRole('heading', { name: /design system/i })).toBeInTheDocument();
    // two color sections now: Direction A + Direction B
    expect(screen.getAllByRole('heading', { name: /colors/i }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('heading', { name: /typography/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /status badges/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /proof.check/i })).toBeInTheDocument();
  });

  it('renders every badge variant in the badge gallery', () => {
    render(<DesignSystemScreen />);
    const gallery = screen.getByTestId('badge-gallery');
    for (const v of BADGE_VARIANTS) {
      expect(within(gallery).getByTestId(`badge-${v}`)).toBeInTheDocument();
    }
  });

  it('shows the Direction-B (light SaaS) swatches scoped via data-direction (CON-001 carry)', () => {
    render(<DesignSystemScreen />);
    const b = screen.getByTestId('swatches-b');
    // the B swatch subtree carries data-direction="b" so its var(--token) resolve to the light theme
    expect(b).toHaveAttribute('data-direction', 'b');
    expect(within(b).getByText('accent')).toBeInTheDocument();
  });

  it('renders all four proof-check chip statuses', () => {
    render(<DesignSystemScreen />);
    const chips = screen.getByTestId('proof-chips');
    for (const s of ['pass', 'fail', 'pending', 'not_applicable']) {
      expect(within(chips).getByLabelText(s)).toBeInTheDocument();
    }
  });
});
