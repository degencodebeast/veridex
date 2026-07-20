import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CanonicalEventStream } from '@/components/screens/cockpit/CanonicalEventStream';
import type { CanonicalEvent } from '@/lib/contracts';

vi.mock('next/navigation', () => ({ usePathname: () => '/arena/wc-fra-bra' }));

// II-W defect 1 · The cockpit→Inspector deep-link MUST key off the backend's `source_sequence_no`
// (the sealed RunEvent.sequence_no that the Inspector endpoint GET /runs/{id}/actions/{seq} looks up
// — veridex/api/router.py:747,778: "`seq` is the decision event's `sequence_no` in the run's event
// log"; the CompetitionEvent carries it as `source_sequence_no` — veridex/competition/events.py:118),
// NOT the transport competition-stream `seq`. When the two DIFFER, linking off the stream `seq` opens
// the WRONG forensic record (or 404s). Honesty: the link must resolve to the sealed action it claims.
describe('II-W defect 1 · Inspector deep-link keys off source_sequence_no, not the stream seq', () => {
  it('an AGENT_ACTION whose stream seq ≠ source_sequence_no links to the SOURCE sequence', () => {
    const events: CanonicalEvent[] = [
      { seq: 87, type: 'AGENT_ACTION', payload_hash: '0xabc123', evidence: true, ts: 1, source_sequence_no: 42 },
    ];
    render(<CanonicalEventStream runId="run_7f3a" events={events} />);
    const link = screen.getByRole('link', { name: /AGENT_ACTION/i });
    // MUST open the sealed source record (42), NEVER the transport stream seq (87).
    expect(link).toHaveAttribute('href', '/inspector/run_7f3a/42');
  });

  it('falls back to the stream seq only when source_sequence_no is absent (legacy/simplified frames)', () => {
    const events: CanonicalEvent[] = [
      { seq: 87, type: 'AGENT_ACTION', payload_hash: '0xabc123', evidence: true, ts: 1 },
    ];
    render(<CanonicalEventStream runId="run_7f3a" events={events} />);
    expect(screen.getByRole('link', { name: /AGENT_ACTION/i })).toHaveAttribute('href', '/inspector/run_7f3a/87');
  });
});
