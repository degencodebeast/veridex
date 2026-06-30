import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StatusBar } from '@/components/layout/StatusBar';
import { StatusBarProvider, usePublishStatus } from '@/components/layout/StatusBarContext';
import type { StatusBarState } from '@/lib/status';

afterEach(() => { vi.unstubAllEnvs(); window.history.replaceState(null, '', '/'); });

function Publisher({ s }: { s: StatusBarState | null }) { usePublishStatus(s); return null; }
function Harness({ publish = null }: { publish?: StatusBarState | null }) {
  return <StatusBarProvider><Publisher s={publish} /><StatusBar /></StatusBarProvider>;
}

const live: StatusBarState = {
  fixture: 'FRA v BRA', competition: 'World Cup', sourceMode: 'live',
  executionMode: 'live_guarded', ws: 'connected', seq: 1284, scoring: true,
};

describe('StatusBar (shared run status)', () => {
  it('is honestly IDLE with no active competition — neutral source, NO fabricated WS CONNECTED', () => {
    render(<Harness />);
    expect(screen.getByTestId('status-source')).toHaveTextContent(/idle/i);
    expect(screen.getByTestId('status-ws')).toHaveTextContent(/idle/i);
    expect(screen.getByTestId('status-ws')).not.toHaveTextContent(/connected/i);
    // verifier + A/B toggle are always present
    expect(screen.getByText(/verifier v0\.9\.2/i)).toBeInTheDocument();
    expect(screen.getByRole('radiogroup', { name: /visual direction/i })).toBeInTheDocument();
  });

  it('shows WS CONNECTED · seq ONLY for a genuinely connected stream', () => {
    render(<Harness publish={live} />);
    expect(screen.getByTestId('status-ws')).toHaveTextContent(/CONNECTED · seq 1284/);
    expect(screen.getByTestId('status-source')).toHaveTextContent(/live/i); // a REAL live stream may show LIVE
  });

  it('NEVER shows CONNECTED for a disconnected stream (honest WS)', () => {
    render(<Harness publish={{ ...live, ws: 'disconnected' }} />);
    expect(screen.getByTestId('status-ws')).toHaveTextContent(/offline/i);
    expect(screen.getByTestId('status-ws')).not.toHaveTextContent(/CONNECTED/i);
  });

  it('MOCK mode populates the bar as REPLAY (demoted), NEVER LIVE, NEVER a fake CONNECTED', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<Harness />); // no publish — the provider's mock seed populates it
    const src = screen.getByTestId('status-source');
    expect(src).toHaveTextContent(/replay/i);
    expect(src).not.toHaveTextContent(/\blive\b/i);
    expect(screen.getByTestId('status-ws')).not.toHaveTextContent(/CONNECTED/i); // mock has no real stream
  });

  it('reuses the A/B DirectionToggle in the bar (flips data-direction) — not forked', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('radio', { name: /SaaS/i }));
    expect(document.documentElement.getAttribute('data-direction')).toBe('b');
    document.documentElement.removeAttribute('data-direction');
  });
});
