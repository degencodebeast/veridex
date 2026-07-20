import { describe, it, expect, vi } from 'vitest';
import { render, screen, within, renderHook, act } from '@testing-library/react';
import {
  ExecutionEvidenceSection,
  useExecutionEvidence,
  type EvState,
  type PageFetcher,
} from './ExecutionEvidenceSection';
import type { ExecutionEvidence } from '@/lib/executionEvidence';
import type { RuntimeEventRecord } from '@/lib/api';

const stub = (state: EvState) => () => state;

const LEGS_APPROVED_ABSTAINED = [
  { kind: 'place_quote', attempted: true, admission: 'APPROVED', execution_status: 'ABSTAINED', frozen: false, possibly_unresolved: false },
  { kind: 'place_quote', attempted: true, admission: 'APPROVED', execution_status: 'ABSTAINED', frozen: false, possibly_unresolved: false },
];

const EVIDENCE: ExecutionEvidence = {
  decisionsObserved: 3,
  byKind: { QUOTE_TWO_SIDED: 2, NO_QUOTE: 1 },
  decisionsWithAttemptedLegs: 2,
  attemptedLegTotal: 4,
  receipts: [
    { id: 10, decision_kind: 'QUOTE_TWO_SIDED', legs: LEGS_APPROVED_ABSTAINED },
    { id: 11, decision_kind: 'NO_QUOTE', legs: [] },
  ],
};

// --- record builders for the live-polling hook test ---
function evRec(id: number, type: string, payload: Record<string, unknown>): RuntimeEventRecord {
  return { id, type, ts: id, channel: 'OPS', run_id: 'run-1', payload } as unknown as RuntimeEventRecord;
}
function legReceiptRec(id: number): RuntimeEventRecord {
  return evRec(id, 'tool_call', {
    telemetry: 'leg_receipt', decision_kind: 'QUOTE_TWO_SIDED',
    legs: [{ kind: 'place_quote', attempted: true, admission: 'APPROVED', execution_status: 'ABSTAINED' }],
  });
}

describe('ExecutionEvidenceSection', () => {
  it('renders honest loading / error / empty states', () => {
    const { rerender } = render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'loading' })} />);
    expect(screen.getByTestId('exec-loading')).toBeInTheDocument();

    rerender(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'error', status: 500 })} />);
    expect(screen.getByTestId('exec-error')).toHaveTextContent(/couldn.t load execution evidence/i);

    const empty: ExecutionEvidence = { decisionsObserved: 0, byKind: {}, decisionsWithAttemptedLegs: 0, attemptedLegTotal: 0, receipts: [] };
    rerender(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: empty })} />);
    expect(screen.getByTestId('exec-empty')).toHaveTextContent(/no execution legs/i);
  });

  it('shows the summary counts including the true attempted-leg total (distinct from decisions-with-legs)', () => {
    render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: EVIDENCE })} />);
    const summary = screen.getByTestId('exec-summary');
    expect(within(summary).getByText('Decisions observed').previousSibling).toHaveTextContent('3');
    expect(within(summary).getByText('Attempted legs (total)').previousSibling).toHaveTextContent('4');
    expect(within(summary).getByText('Decisions with ≥1 attempted leg').previousSibling).toHaveTextContent('2');
    expect(within(summary).getByText('QUOTE_TWO_SIDED')).toBeInTheDocument();
  });

  it('renders leg fields VERBATIM — ABSTAINED shown, never relabeled to FILLED/SUCCESS; empty legs get no receipt', () => {
    render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: EVIDENCE })} />);
    const receipts = screen.getByTestId('exec-receipts');
    expect(within(receipts).getAllByText('EXECUTION: ABSTAINED').length).toBe(2); // one per attempted leg
    expect(within(receipts).getAllByText('ADMISSION: APPROVED').length).toBe(2);
    expect(within(receipts).queryByText(/FILLED|SUCCESS|SIMULATED FILL|EXECUTED/)).toBeNull();
    expect(within(receipts).getByText(/no execution leg was produced/i)).toBeInTheDocument();
  });

  it('carries the honest headline when a leg WAS attempted (attempted, not submitted; no live money)', () => {
    render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: EVIDENCE })} />);
    const headline = screen.getByTestId('exec-headline');
    expect(headline).toHaveTextContent(/NOT SUBMITTED TO A VENUE/);
    expect(headline).toHaveTextContent(/no live money moved/i);
  });

  it('MAJOR-2: an all-no-leg run (decisions>0, attemptedLegTotal=0) never claims an attempted leg', () => {
    const noLeg: ExecutionEvidence = {
      decisionsObserved: 2, byKind: { NO_QUOTE: 1, HOLD: 1 }, decisionsWithAttemptedLegs: 0, attemptedLegTotal: 0,
      receipts: [{ id: 1, decision_kind: 'NO_QUOTE', legs: [] }, { id: 2, decision_kind: 'HOLD', legs: [] }],
    };
    render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: noLeg })} />);
    const headline = screen.getByTestId('exec-headline');
    expect(headline).toHaveTextContent(/NO EXECUTION LEG ATTEMPTED/);
    expect(headline).not.toHaveTextContent(/ATTEMPTED DRY-RUN LEG/); // never claim a leg the rows contradict
  });

  it('caps the rendered receipt list and honestly discloses the truncation', () => {
    const receipts = Array.from({ length: 45 }, (_, i) => ({ id: i + 1, decision_kind: 'NO_QUOTE', legs: [] }));
    const big: ExecutionEvidence = { decisionsObserved: 45, byKind: { NO_QUOTE: 45 }, decisionsWithAttemptedLegs: 0, attemptedLegTotal: 0, receipts };
    render(<ExecutionEvidenceSection instanceId="i" useEvidence={stub({ kind: 'ready', evidence: big })} />);
    expect(within(screen.getByTestId('exec-receipts')).getAllByText(/no execution leg/i)).toHaveLength(40);
    expect(screen.getByTestId('exec-truncation')).toHaveTextContent(/first 40 of 45/i);
  });
});

describe('useExecutionEvidence (live cursor-polling)', () => {
  it('MAJOR-1: a short first page is NOT terminal — polling surfaces later receipts without a remount', async () => {
    vi.useFakeTimers();
    try {
      // First catch-up: empty (run still writing). Later: receipts + the authoritative run_completed.
      const batches: RuntimeEventRecord[][] = [
        [],
        [legReceiptRec(10), evRec(11, 'run_completed', {})],
      ];
      let call = 0;
      const fetchPage: PageFetcher = async () => batches[Math.min(call++, batches.length - 1)];

      const { result } = renderHook(() => useExecutionEvidence('inst', fetchPage));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); }); // initial tick
      // empty + NOT terminal → stays loading (never a false "no legs")
      expect(result.current.kind).toBe('loading');

      await act(async () => { await vi.advanceTimersByTimeAsync(3000); }); // next poll delivers the receipts
      expect(result.current.kind).toBe('ready');
      if (result.current.kind === 'ready') {
        expect(result.current.evidence.decisionsObserved).toBe(1);
        expect(result.current.evidence.attemptedLegTotal).toBe(1);
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('MINOR: single-flight — a slow poll never overlaps the next tick (recursive scheduling)', async () => {
    vi.useFakeTimers();
    try {
      let releaseFirst!: () => void;
      let call = 0;
      const fetchPage: PageFetcher = () => {
        call += 1;
        if (call === 1) return new Promise<RuntimeEventRecord[]>((r) => { releaseFirst = () => r([]); });
        return Promise.resolve([legReceiptRec(10), evRec(11, 'run_completed', {})]);
      };
      renderHook(() => useExecutionEvidence('inst', fetchPage));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); }); // first tick starts, awaits releaseFirst
      expect(call).toBe(1);
      // Advancing far past the interval must NOT start a second request while the first is still pending.
      await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
      expect(call).toBe(1);
      // Settle the first (empty, non-terminal) → the NEXT poll is scheduled and delivers the terminal batch.
      await act(async () => { releaseFirst(); await vi.advanceTimersByTimeAsync(3000); });
      expect(call).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
