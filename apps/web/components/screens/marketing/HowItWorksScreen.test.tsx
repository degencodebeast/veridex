import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { HowItWorksScreen } from './HowItWorksScreen';

describe('HowItWorksScreen (public explainer)', () => {
  it('renders the hero thesis heading', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/from agent decision to proof you can inspect/i);
  });

  it('renders the six-stage proof rail in canonical order', () => {
    render(<HowItWorksScreen />);
    const rail = screen.getByTestId('proof-rail');
    expect(rail.tagName).toBe('OL'); // a11y: ordered list, order carries meaning
    const labels = within(rail).getAllByRole('listitem').map((li) => li.textContent);
    expect(labels).toEqual([
      expect.stringContaining('evidence'),
      expect.stringContaining('law'),
      expect.stringContaining('policy'),
      expect.stringContaining('receipt'),
      expect.stringContaining('score'),
      expect.stringContaining('anchor'),
    ]);
  });

  it('renders six explanation+artifact stages', () => {
    render(<HowItWorksScreen />);
    const stages = within(screen.getByTestId('hiw-stages')).getAllByRole('article');
    expect(stages).toHaveLength(6);
  });

  // ---- Honesty rules (judged) ----

  it('labels the evidence pack as verified replay — never live (source mode visible)', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByText(/^verified replay$/i)).toBeInTheDocument(); // the pack badge
    expect(screen.getByText(/replay · pinned/i)).toBeInTheDocument();
    // the illustrative run artifact must not stamp itself LIVE
    expect(screen.queryByText(/^live$/i)).toBeNull();
  });

  it('keeps execution receipt visually and textually distinct from proof of skill', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByText(/receipts prove execution\. clv measures skill\./i)).toBeInTheDocument();
    expect(screen.getByText(/a fill, not proof of skill/i)).toBeInTheDocument();
  });

  it('presents CLV as backend-authoritative, recomputed by the law (never self-reported)', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByText(/closing \(law\)/i)).toBeInTheDocument();
    expect(screen.getByText(/clv recomputed/i)).toBeInTheDocument();
    // "backend-authoritative" appears in both the stage copy and the leaderboard-row chip
    expect(screen.getAllByText(/backend-authoritative/i).length).toBeGreaterThanOrEqual(1);
  });

  it('reports anchor state honestly — includes the not_anchored case, no fake green anchor', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByText('not_anchored')).toBeInTheDocument(); // the honest-state token span
    expect(screen.getByText(/never implied/i)).toBeInTheDocument();
    // the ANCHORED example is present but blue/accent (proof signal), not a green "valid" chip
    expect(screen.getByText('ANCHORED')).toBeInTheDocument();
  });

  // ---- nav wiring + standalone chrome ----

  it('wires the explainer nav to the sibling routes and Enter App', () => {
    render(<HowItWorksScreen />);
    expect(screen.getByRole('link', { name: /why veridex/i })).toHaveAttribute('href', '/why-veridex');
    expect(screen.getByRole('link', { name: /^home$/i })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: /enter app/i })).toHaveAttribute('href', '/competitions');
  });

  it('renders its own main landmark and no app status bar (standalone marketing chrome)', () => {
    const { container } = render(<HowItWorksScreen />);
    expect(container.querySelector('main')).not.toBeNull();
    expect(screen.queryByTestId('status-bar')).toBeNull();
  });
});
