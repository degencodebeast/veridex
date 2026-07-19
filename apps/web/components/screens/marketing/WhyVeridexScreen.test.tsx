import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { WhyVeridexScreen } from './WhyVeridexScreen';

describe('WhyVeridexScreen (public explainer)', () => {
  it('renders the hero thesis heading', () => {
    render(<WhyVeridexScreen />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/independent scoreboard/i);
  });

  it('renders the three trust boundaries: intelligence → execution → verification', () => {
    render(<WhyVeridexScreen />);
    const boundaries = within(screen.getByTestId('trust-boundaries')).getAllByRole('listitem');
    expect(boundaries).toHaveLength(3);
    expect(screen.getByText(/01 · INTELLIGENCE/)).toBeInTheDocument();
    expect(screen.getByText(/02 · EXECUTION/)).toBeInTheDocument();
    expect(screen.getByText(/03 · VERIFICATION/)).toBeInTheDocument();
  });

  it('contrasts an unsupported claim against the proof chain', () => {
    render(<WhyVeridexScreen />);
    const contrast = screen.getByTestId('claim-contrast');
    expect(within(contrast).getByText(/unsupported claim/i)).toBeInTheDocument();
    expect(within(contrast).getByText(/veridex proof chain/i)).toBeInTheDocument();
    expect(within(contrast).getByText(/evidence → law → policy → receipt → score → anchor/)).toBeInTheDocument();
  });

  it('renders the four structural pillars', () => {
    render(<WhyVeridexScreen />);
    expect(within(screen.getByTestId('pillars')).getAllByRole('listitem')).toHaveLength(4);
  });

  // ---- Honesty rules (judged) ----

  it('renders the honesty ledger with both columns, incl. the not_anchored guarantee', () => {
    render(<WhyVeridexScreen />);
    const proves = screen.getByTestId('ledger-proves');
    const notClaim = screen.getByTestId('ledger-not-claim');
    expect(within(proves).getByText(/veridex proves/i)).toBeInTheDocument();
    expect(within(notClaim).getByText(/does not claim/i)).toBeInTheDocument();
    // honesty-critical rows
    expect(within(notClaim).getByText(/that replay data was live/i)).toBeInTheDocument();
    expect(within(notClaim).getByText(/that a receipt proves alpha/i)).toBeInTheDocument();
    expect(within(notClaim).getByText(/that simulated pnl is settled pnl/i)).toBeInTheDocument();
    expect(within(notClaim).getByText(/that not_anchored means anchored/i)).toBeInTheDocument();
  });

  it('comparison matrix uses only neutral capability labels — no fabricated competitor names', () => {
    render(<WhyVeridexScreen />);
    const matrix = screen.getByTestId('comparison-matrix');
    // neutral vocabulary present
    expect(within(matrix).getAllByText(/not ordinarily provided/i).length).toBeGreaterThan(0);
    expect(within(matrix).getAllByText(/not independently verifiable/i).length).toBeGreaterThan(0);
    // neutral column headers, no invented brand names
    expect(within(matrix).getByText(/self-reported leaderboard/i)).toBeInTheDocument();
    expect(within(matrix).getByText(/picks \/ signals feed/i)).toBeInTheDocument();
    expect(within(matrix).queryByText(/recall|oddsflow|clawsport|polymarket|betfair/i)).toBeNull();
    // honest anchor row present
    expect(within(matrix).getByText(/incl\. not_anchored/i)).toBeInTheDocument();
  });

  it('renders three audience value bands', () => {
    render(<WhyVeridexScreen />);
    const bands = screen.getByTestId('audience-bands');
    expect(within(bands).getByText(/for trading desks/i)).toBeInTheDocument();
    expect(within(bands).getByText(/for agent builders/i)).toBeInTheDocument();
    expect(within(bands).getByText(/for judges & ops/i)).toBeInTheDocument();
  });

  // ---- nav wiring + standalone chrome ----

  it('wires the explainer nav to the sibling routes and Enter App', () => {
    render(<WhyVeridexScreen />);
    expect(screen.getByRole('link', { name: /^how it works$/i })).toHaveAttribute('href', '/how-it-works');
    expect(screen.getByRole('link', { name: /^home$/i })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: /enter app/i })).toHaveAttribute('href', '/competitions');
  });

  it('renders its own main landmark and no app status bar (standalone marketing chrome)', () => {
    const { container } = render(<WhyVeridexScreen />);
    expect(container.querySelector('main')).not.toBeNull();
    expect(screen.queryByTestId('status-bar')).toBeNull();
  });
});
