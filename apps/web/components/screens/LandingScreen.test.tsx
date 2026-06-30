import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { LandingScreen } from '@/components/screens/LandingScreen';

function stubMatchMedia(reduced: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: reduced && /reduce/.test(q), media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(),
    removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
}
beforeEach(() => stubMatchMedia(false));

describe('LandingScreen (V4 fidelity)', () => {
  it('renders the V4 two-line hero headline', () => {
    render(<LandingScreen />);
    const h1 = screen.getByRole('heading', { level: 1 });
    expect(h1).toHaveTextContent(/agents can trade/i);
    expect(h1).toHaveTextContent(/grade themselves/i);
  });

  it('renders the PROOF TRACE with all six named steps in order — an ordered list, not controls', () => {
    render(<LandingScreen />);
    const trace = screen.getByTestId('proof-trace');
    expect(trace.tagName).toBe('OL'); // a11y: ordered list, not interactive controls
    const steps = within(trace).getAllByTestId('trace-step');
    expect(steps.map((s) => s.getAttribute('data-step'))).toEqual(
      ['evidence', 'law', 'policy', 'receipt', 'score', 'anchor'],
    );
    expect(within(trace).queryByRole('button')).toBeNull();
  });

  it('does NOT render the fabricated stat band (no honest source → omitted)', () => {
    render(<LandingScreen />);
    expect(screen.queryByText('48')).toBeNull();
    expect(screen.queryByText('122')).toBeNull();
    expect(screen.queryByText(/82\.5k/)).toBeNull();
    expect(screen.queryByText(/PRIZE TVL/i)).toBeNull();
    expect(screen.queryByText(/ANCHORED RUNS/i)).toBeNull();
  });

  it('keeps honest Phase-2D payout language in the closing CTA (SEC-008 — no implied live payouts)', () => {
    render(<LandingScreen />);
    const cta = screen.getByTestId('prize-cta');
    expect(within(cta).getByText(/payout state is always labeled honestly/i)).toBeInTheDocument();
    expect(within(cta).getByText(/2D/i)).toBeInTheDocument();
  });

  it('reduced-motion renders the proof trace instantly — reveal disabled', () => {
    stubMatchMedia(true);
    render(<LandingScreen />);
    expect(screen.getByTestId('proof-trace')).toHaveAttribute('data-reveal', 'instant');
  });

  it('renders its own main landmark (standalone marketing page, outside AppShell)', () => {
    const { container } = render(<LandingScreen />);
    expect(container.querySelector('main')).not.toBeNull();
  });

  it('renders the wordmark nav (VERIDEX + PROOF ARENA tag) and an Enter App entry', () => {
    render(<LandingScreen />);
    expect(screen.getAllByText('VERIDEX').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('PROOF ARENA')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /enter app/i }).length).toBeGreaterThanOrEqual(1);
  });

  it('renders four numbered WHY cards and three HOW-IT-WORKS step cards', () => {
    render(<LandingScreen />);
    expect(within(screen.getByTestId('why-veridex')).getAllByRole('listitem')).toHaveLength(4);
    expect(within(screen.getByTestId('how-it-works')).getAllByRole('listitem')).toHaveLength(3);
  });

  it('keeps the GENERIC comparison (self-reported bots) — no fabricated named competitors', () => {
    render(<LandingScreen />);
    expect(screen.getByText(/self-reported bots/i)).toBeInTheDocument();
    expect(screen.queryByText(/Recall|OddsFlow|ClawSportBot/)).toBeNull();
  });
});
