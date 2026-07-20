import { describe, it, expect } from 'vitest';
import { projectExecutionEvidence } from '@/lib/executionEvidence';
import type { RuntimeEventRecord } from '@/lib/api';

// Minimal record builder — only the fields the projection reads (id, type, payload).
function ev(id: number, type: string, payload: Record<string, unknown>): RuntimeEventRecord {
  return { id, type, ts: id, channel: 'OPS', run_id: 'run-1', payload } as unknown as RuntimeEventRecord;
}

function legReceipt(id: number, decision_kind: string, legs: Record<string, unknown>[]): RuntimeEventRecord {
  return ev(id, 'tool_call', { telemetry: 'leg_receipt', decision_kind, legs });
}

const APPROVED_ABSTAINED = {
  kind: 'place_quote', attempted: true, admission: 'APPROVED',
  execution_status: 'ABSTAINED', frozen: false, possibly_unresolved: false,
};

describe('projectExecutionEvidence', () => {
  it('projects only leg_receipt tool_call events — ignores action_emitted / status / non-receipt tool_calls', () => {
    const out = projectExecutionEvidence([
      ev(1, 'action_emitted', { action: 'QUOTE' }),
      ev(2, 'status_changed', { status: 'running' }),
      ev(3, 'tool_call', { telemetry: 'something_else' }),
      legReceipt(4, 'QUOTE_TWO_SIDED', [APPROVED_ABSTAINED, APPROVED_ABSTAINED]),
    ]);
    expect(out.decisionsObserved).toBe(1);
    expect(out.receipts).toHaveLength(1);
    expect(out.receipts[0].decision_kind).toBe('QUOTE_TWO_SIDED');
  });

  it('counts by decision_kind and orders receipts by durable id (not ts)', () => {
    const out = projectExecutionEvidence([
      legReceipt(30, 'NO_QUOTE', []),
      legReceipt(10, 'QUOTE_TWO_SIDED', [APPROVED_ABSTAINED, APPROVED_ABSTAINED]),
      legReceipt(20, 'HOLD', []),
    ]);
    expect(out.byKind).toEqual({ QUOTE_TWO_SIDED: 1, NO_QUOTE: 1, HOLD: 1 });
    expect(out.receipts.map((r) => r.id)).toEqual([10, 20, 30]); // id order, not insertion/ts
  });

  it('attemptedLegTotal sums attempted legs (a two-sided quote = 2), distinct from decisions-with-legs', () => {
    const out = projectExecutionEvidence([
      legReceipt(1, 'QUOTE_TWO_SIDED', [APPROVED_ABSTAINED, APPROVED_ABSTAINED]), // 2 attempted
      legReceipt(2, 'QUOTE_TWO_SIDED', [APPROVED_ABSTAINED, APPROVED_ABSTAINED]), // 2 attempted
      legReceipt(3, 'NO_QUOTE', []),
    ]);
    expect(out.decisionsWithAttemptedLegs).toBe(2); // two DECISIONS carried legs
    expect(out.attemptedLegTotal).toBe(4); // but FOUR attempted legs total (2 per two-sided quote)
  });

  it('renders enum fields verbatim (no relabeling of ABSTAINED)', () => {
    const out = projectExecutionEvidence([legReceipt(1, 'QUOTE_TWO_SIDED', [APPROVED_ABSTAINED])]);
    const leg = out.receipts[0].legs[0];
    expect(leg).toEqual({
      kind: 'place_quote', attempted: true, admission: 'APPROVED',
      execution_status: 'ABSTAINED', frozen: false, possibly_unresolved: false,
    });
  });

  it('empty-leg decisions (NO_QUOTE/HOLD) contribute no attempted legs', () => {
    const out = projectExecutionEvidence([legReceipt(1, 'NO_QUOTE', []), legReceipt(2, 'HOLD', [])]);
    expect(out.decisionsWithAttemptedLegs).toBe(0);
    expect(out.attemptedLegTotal).toBe(0);
    expect(out.receipts.every((r) => r.legs.length === 0)).toBe(true);
  });
});
