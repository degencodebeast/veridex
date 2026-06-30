import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { LandingScreen } from '@/components/screens/LandingScreen';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('LandingScreen (REQ-010)', () => {
  it('renders the hero promise', () => {
    render(<LandingScreen />);
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
    expect(screen.getByText(/cannot self-certify/i)).toBeInTheDocument();
  });

  it('renders exactly four differentiators', () => {
    render(<LandingScreen />);
    const diffs = screen.getByTestId('differentiators');
    expect(within(diffs).getAllByRole('listitem')).toHaveLength(4);
  });

  it('renders a competitor comparison table and a how-it-works strip', () => {
    render(<LandingScreen />);
    expect(screen.getByTestId('competitor-table')).toBeInTheDocument();
    expect(screen.getByTestId('how-it-works')).toBeInTheDocument();
  });

  it('renders the prize-vault teaser with an honest 2D label (SEC-008)', () => {
    render(<LandingScreen />);
    const vault = screen.getByTestId('prize-vault');
    // Both the honest note ("Phase 2D.") and the pending badge ("2D
    // implementation") carry the 2D label; assert at least one is present.
    expect(within(vault).getAllByText(/2D/i).length).toBeGreaterThan(0);
  });

  it('renders the CTAs with correct destinations', () => {
    render(<LandingScreen />);
    expect(screen.getByRole('link', { name: /Enter the Arena/i })).toHaveAttribute('href', '/arena');
    expect(screen.getByRole('link', { name: /Enter App/i })).toHaveAttribute('href', '/competitions');
    expect(screen.getByRole('button', { name: /Connect Wallet/i })).toBeInTheDocument();
  });

  it('does NOT emit its own <main> (AppShell owns the single main landmark)', () => {
    const { container } = render(<LandingScreen />);
    expect(container.querySelector('main')).toBeNull();
  });
});
