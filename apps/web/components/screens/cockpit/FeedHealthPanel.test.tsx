import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FeedHealthPanel } from '@/components/screens/cockpit/FeedHealthPanel';
import type { FeedHealthState } from '@/lib/contracts';

// T10 AC-2D-104: the feed-health panel binds a FeedHealthReport (source_mode, connected,
// staleness_s, stale, ticks_seen, fixture_id) and MUST render disconnect/reconnect/stale states
// honestly — never a frozen "live-looking" view when the feed isn't actually fresh.
const HEALTHY: FeedHealthState = {
  source_mode: 'live', ws_live: true, connected: true, txline_configured: true,
  events_per_min: 12, ticks_seen: 40, staleness_s: 2, stale: false, fixture_id: 18172280,
  anchor_status: 'pending', last_tick_ts: 100,
};

describe('FeedHealthPanel (T10 AC-2D-104)', () => {
  it('renders a healthy connected feed with ticks_seen and fixture_id', () => {
    render(<FeedHealthPanel feedHealth={HEALTHY} wsStatus="connected" />);
    expect(screen.getByTestId('feed-ok')).toBeInTheDocument();
    expect(screen.getByText(/40/)).toBeInTheDocument();
    expect(screen.getByText(/18172280/)).toBeInTheDocument();
    expect(screen.queryByTestId('feed-stale')).toBeNull();
  });

  it('renders the honest stale state when the FeedHealthReport says stale — never hidden', () => {
    render(<FeedHealthPanel feedHealth={{ ...HEALTHY, stale: true, staleness_s: 90 }} wsStatus="connected" />);
    expect(screen.getByTestId('feed-stale')).toBeInTheDocument();
    expect(screen.getByText(/90/)).toBeInTheDocument();
  });

  it('renders a visible reconnecting state on wsStatus=reconnecting, not a frozen live view', () => {
    render(<FeedHealthPanel feedHealth={{ ...HEALTHY, stale: false }} wsStatus="reconnecting" />);
    expect(screen.getByTestId('feed-stale')).toBeInTheDocument();
    expect(screen.getByText(/reconnect/i)).toBeInTheDocument();
  });

  it('renders a visible disconnected state on wsStatus=disconnected', () => {
    render(<FeedHealthPanel feedHealth={{ ...HEALTHY, connected: false }} wsStatus="disconnected" />);
    expect(screen.getByTestId('feed-stale')).toBeInTheDocument();
  });

  it('derives the LIVE/REPLAY label from source_mode only, never a hardcoded constant', () => {
    const { rerender } = render(<FeedHealthPanel feedHealth={{ ...HEALTHY, source_mode: 'live' }} wsStatus="connected" />);
    expect(screen.getByText('LIVE')).toBeInTheDocument();
    rerender(<FeedHealthPanel feedHealth={{ ...HEALTHY, source_mode: 'replay' }} wsStatus="connected" />);
    expect(screen.getByText('REPLAY')).toBeInTheDocument();
  });
});
