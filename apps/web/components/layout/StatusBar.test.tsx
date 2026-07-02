import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StatusBar } from '@/components/layout/StatusBar';
import { StatusBarProvider, usePublishStatus } from '@/components/layout/StatusBarContext';
import { VERIFIER_VERSION } from '@/lib/status';
import { MOCK_FIXTURES } from '@/lib/mock';
import type { StatusBarState } from '@/lib/status';

// The verifier version the Proof Card shows under mock — the single value the bar must agree with.
const ARTIFACT_VERIFIER = MOCK_FIXTURES.competition.proof_card?.verifier_version ?? '';
// END-anchored so "v0" can NOT substring-match a fabricated "v0.9.2" (genuine teeth, not vacuous).
const verifierRe = (v: string) =>
  new RegExp(`verifier ${v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'i');

afterEach(() => { vi.unstubAllEnvs(); window.history.replaceState(null, '', '/'); });

function Publisher({ s }: { s: StatusBarState | null }) { usePublishStatus(s); return null; }
function Harness({ publish = null }: { publish?: StatusBarState | null }) {
  return <StatusBarProvider><Publisher s={publish} /><StatusBar /></StatusBarProvider>;
}

const live: StatusBarState = {
  fixture: 'FRA v BRA', competition: 'World Cup', sourceMode: 'live',
  executionMode: 'live_guarded', ws: 'connected', seq: 1284, scoring: true,
  verifierVersion: ARTIFACT_VERIFIER,
};

describe('StatusBar (shared run status)', () => {
  it('is honestly IDLE with no active competition — neutral source, NO fabricated WS CONNECTED', () => {
    render(<Harness />);
    expect(screen.getByTestId('status-source')).toHaveTextContent(/idle/i);
    expect(screen.getByTestId('status-ws')).toHaveTextContent(/idle/i);
    expect(screen.getByTestId('status-ws')).not.toHaveTextContent(/connected/i);
    // verifier + A/B toggle are always present; idle falls back to the canonical const
    expect(screen.getByText(verifierRe(VERIFIER_VERSION))).toBeInTheDocument();
    expect(screen.getByRole('radiogroup', { name: /visual direction/i })).toBeInTheDocument();
  });

  it('SINGLE SOURCE: the canonical verifier const matches the fixtures — no independent v0.9.2 fabrication', () => {
    // The bar/Landing const, the competition proof_card, and the proof artifact must all agree,
    // so the status bar can never show a verifier the Proof Card contradicts.
    expect(ARTIFACT_VERIFIER).toBeTruthy();
    expect(VERIFIER_VERSION).toBe(ARTIFACT_VERIFIER);
    expect(VERIFIER_VERSION).toBe(MOCK_FIXTURES.proofArtifact.verifier_version);
  });

  it('MOCK: the bar shows the SAME verifier the proof artifact carries (consistency, not a literal)', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<Harness />); // the provider's mock seed derives verifier from the competition proof_card
    expect(screen.getByText(verifierRe(ARTIFACT_VERIFIER))).toBeInTheDocument();
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
