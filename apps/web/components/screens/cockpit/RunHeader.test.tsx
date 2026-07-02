import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RunHeader } from '@/components/screens/cockpit/RunHeader';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('RunHeader (REQ-011 run header)', () => {
  it('renders fixture, source/exec/proof modes and WS status', () => {
    render(<RunHeader header={sampleCockpitState.header} wsStatus="connected" />);
    expect(screen.getByText(/FRA v BRA/)).toBeInTheDocument();
    // exact badge/pill strings — the InfoTip glossary popovers also contain "live"/"paper"/"verified"
    // as substrings (they're in the DOM for aria-describedby), so scope to the exact chip text.
    expect(screen.getByText('Live')).toBeInTheDocument();        // source-mode badge
    expect(screen.getByText('PAPER')).toBeInTheDocument();       // exec-mode pill
    expect(screen.getByText('Verified')).toBeInTheDocument();    // proof-mode badge
    expect(screen.getByText(/connected/i)).toBeInTheDocument();  // WS status
  });

  it('flags a degraded WebSocket connection honestly', () => {
    render(<RunHeader header={sampleCockpitState.header} wsStatus="reconnecting" />);
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
  });

  it('renders valid_pct as a percent (0-100), matching the wire convention', () => {
    // sample header valid_pct is 93 (percent); must render "93% valid", not 9300%.
    render(<RunHeader header={{ ...sampleCockpitState.header, valid_pct: 93 }} wsStatus="connected" />);
    expect(screen.getByText(/93% valid/)).toBeInTheDocument();
    expect(screen.queryByText(/9300/)).toBeNull();
  });
});
