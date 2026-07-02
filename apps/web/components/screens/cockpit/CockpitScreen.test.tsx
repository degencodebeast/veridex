import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CockpitScreen } from '@/components/screens/cockpit/CockpitScreen';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import { GLOSSARY } from '@/lib/glossary';
import type { FeedHealthState } from '@/lib/contracts';

const SAMPLE_FEED_HEALTH: FeedHealthState = {
  source_mode: 'live', ws_live: true, connected: true, txline_configured: true,
  events_per_min: 12, ticks_seen: 40, staleness_s: 2, stale: false, fixture_id: 18172280,
  anchor_status: 'pending', last_tick_ts: 100,
};

vi.mock('next/navigation', () => ({ usePathname: () => '/arena/wc-fra-bra' }));
vi.mock('@/hooks/useArenaStream', () => ({
  useArenaStream: () => ({ state: sampleCockpitState, wsStatus: 'connected', feedHealth: SAMPLE_FEED_HEALTH }),
}));

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('CockpitScreen (REQ-011 assembly)', () => {
  it('assembles header, trace, match-state, leaderboard, event stream, exec lane, policy', () => {
    render(<CockpitScreen competitionId="wc-fra-bra" initial={sampleCockpitState} />);
    expect(screen.getByText(/FRA v BRA/)).toBeInTheDocument();            // RunHeader
    expect(screen.getByText(/projection of the canonical log/i)).toBeInTheDocument(); // ProofTraceStrip
    expect(screen.getByLabelText('Match state')).toBeInTheDocument();      // MatchStatePanel
    expect(screen.getByLabelText('CLV leaderboard')).toBeInTheDocument();  // ClvLeaderboard
    expect(screen.getByLabelText('Canonical event stream')).toBeInTheDocument();
    expect(screen.getByLabelText('Execution lane')).toBeInTheDocument();
    expect(screen.getByLabelText('Policy decisions')).toBeInTheDocument();
    expect(screen.getByLabelText('Feed health')).toBeInTheDocument(); // T10: FeedHealthPanel
  });

  it('surfaces a deep-linkable AGENT_ACTION row (start of the killer flow — AC-021)', () => {
    render(<CockpitScreen competitionId="wc-fra-bra" initial={sampleCockpitState} />);
    expect(screen.getByRole('link', { name: /AGENT_ACTION/i })).toHaveAttribute('href', '/inspector/run_7f3a/87');
  });

  it('InfoTip copy is single-sourced from lib/glossary.ts — no per-panel microcopy drift', () => {
    render(<CockpitScreen competitionId="wc-fra-bra" initial={sampleCockpitState} />);
    // the cockpit panels pull glossary text verbatim (RunHeader / ClvLeaderboard / event stream)
    expect(screen.getByText(GLOSSARY.clv.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.source_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.proof_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.seq.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.anchor.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.checks_vs_metrics.definition)).toBeInTheDocument();
  });
});
