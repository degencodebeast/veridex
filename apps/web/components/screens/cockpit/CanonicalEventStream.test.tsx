import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CanonicalEventStream } from '@/components/screens/cockpit/CanonicalEventStream';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import { GLOSSARY } from '@/lib/glossary';
import type { CanonicalEvent } from '@/lib/contracts';

vi.mock('next/navigation', () => ({ usePathname: () => '/arena/wc-fra-bra' }));

const MIXED: CanonicalEvent[] = [
  { seq: 2, type: 'AGENT_ACTION', payload_hash: '0xaaaaaa', evidence: true, ts: 2 },
  { seq: 1, type: 'score_update', payload_hash: '0xbbbbbb', evidence: false, ts: 1 },
];

describe('CanonicalEventStream (REQ-011 / AC-021)', () => {
  it('marks each row ● sealed-evidence or ○ ui-only — the honest evidence-prefix flag, never faked', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={MIXED} />);
    const flags = screen.getAllByTestId('ev-flag');
    // sealed evidence prefix (AGENT_ACTION) is ● evidence; the derived non-scoring tail is ○ ui-only
    expect(flags.some((f) => f.textContent?.includes('●') && /evidence/i.test(f.textContent ?? ''))).toBe(true);
    expect(flags.some((f) => f.textContent?.includes('○') && /ui-only/i.test(f.textContent ?? ''))).toBe(true);
  });

  it('renders seq, type, payload_hash and evidence flag per row', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(screen.getByText('87')).toBeInTheDocument();      // seq
    expect(screen.getByText('AGENT_ACTION')).toBeInTheDocument();
    expect(screen.getAllByText(/0x/).length).toBeGreaterThan(0); // payload_hash
  });

  it('makes AGENT_ACTION rows deep-link to the Decision Inspector for that action', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    const link = screen.getByRole('link', { name: /AGENT_ACTION/i });
    expect(link).toHaveAttribute('href', '/inspector/run_7f3a/87');
  });

  it('does not deep-link non-AGENT_ACTION rows', () => {
    render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(screen.queryByRole('link', { name: /law_recomputed/i })).toBeNull();
  });

  it('applies no per-row entrance animation class (PAT-003/AC-031)', () => {
    const { container } = render(<CanonicalEventStream runId="run_7f3a" events={sampleCockpitState.events} />);
    expect(container.querySelector('[class*="enter"], [class*="animate"]')).toBeNull();
  });

  // T10 AC-2D-103: a live score row carrying window_clv_bps must render the "window CLV" glossary
  // label — and must NEVER render the plain "CLV" label, so a spectator can't mistake an in-play
  // window value for the true closing CLV.
  it('renders the "window CLV" label (not "CLV") for a row carrying window_clv_bps', () => {
    const rows: CanonicalEvent[] = [
      { seq: 1, type: 'law_result', payload_hash: '0x1', evidence: false, ts: 1, clv: { kind: 'window_clv', bps: 7 } },
    ];
    render(<CanonicalEventStream runId="run_7f3a" events={rows} />);
    expect(screen.getByText(new RegExp(GLOSSARY.window_clv.label, 'i'))).toBeInTheDocument();
    expect(screen.queryByText(/^CLV /)).toBeNull();
  });

  it('renders the plain "CLV" label for a row carrying true clv_bps', () => {
    const rows: CanonicalEvent[] = [
      { seq: 1, type: 'law_result', payload_hash: '0x1', evidence: false, ts: 1, clv: { kind: 'clv', bps: 18 } },
    ];
    render(<CanonicalEventStream runId="run_7f3a" events={rows} />);
    expect(screen.getByText(new RegExp(`^${GLOSSARY.clv.label}`))).toBeInTheDocument();
  });

  it('renders an honest pending state for a pending_horizon row — never a fabricated number', () => {
    const rows: CanonicalEvent[] = [
      { seq: 1, type: 'law_result', payload_hash: '0x1', evidence: false, ts: 1, clv: { kind: 'pending' } },
    ];
    render(<CanonicalEventStream runId="run_7f3a" events={rows} />);
    expect(screen.getByText(GLOSSARY.clv_pending.label)).toBeInTheDocument();
    expect(screen.queryByText(/bps/)).toBeNull();
  });
});
