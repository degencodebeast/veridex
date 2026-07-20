'use client';
import { useEffect, useState } from 'react';
import { ApiError, getRuntimeEvents, type RuntimeEventRecord } from '@/lib/api';
import {
  projectExecutionEvidence,
  type ExecutionEvidence,
  RECEIPT_HEADLINE,
  NO_ATTEMPTED_HEADLINE,
  RECEIPT_SUPPORTING,
  NO_LEG_COPY,
} from '@/lib/executionEvidence';
import styles from './InstanceScreen.module.css';

// Step-2 projection: the OWNER-scoped action -> attempted-dry-run-leg -> honest receipt chain for one
// deployed instance. Reads the run-scoped runtime-events feed (GET /agents/instances/{id}/runtime-events
// — run_id-scoped server-side, so no cross-run/owner OPS bleed) by durable-id cursor, projects the
// leg_receipt events VERBATIM, and renders honest loading/empty/error states — never a fixture fallback.

const PAGE = 250; // bounded page; accumulate/dedupe by durable id, advance the cursor, until a short batch
const RECEIPT_CAP = 40; // rows rendered (summary above reflects ALL); labeled — never a silent truncation
const POLL_INTERVAL_MS = 2500; // cursor-poll while the run is still writing (deploy launches it async)

type EvState =
  | { kind: 'loading' } // no data yet AND run not terminal — never rendered as "no legs"
  | { kind: 'ready'; evidence: ExecutionEvidence }
  | { kind: 'error'; status?: number };

// exported for tests
export type { EvState };

// A run's authoritative end — once present, the feed is complete and polling can stop. A short page is
// only "caught up now" (the run may still be writing), so it is NEVER treated as terminal on its own.
function isRunTerminal(events: readonly RuntimeEventRecord[]): boolean {
  return events.some((e) => e.type === 'run_completed' || e.type === 'run_failed');
}

export type PageFetcher = (id: string, since: number, limit: number) => Promise<RuntimeEventRecord[]>;

/**
 * Cursor-poll the run's durable OPS feed and project it live.
 *
 * The deploy launches the run asynchronously and the UI navigates straight to the instance page, so a
 * one-shot fetch can catch the run mid-write and under-count. Instead we page from an exclusive `id`
 * cursor, accumulate/dedupe by id, and re-poll on an interval until an authoritative run_completed/
 * run_failed event arrives (or an error). The mounted UI updates in place — no remount/refresh. Resets
 * cleanly on an instance switch; tears down the interval on unmount.
 */
export function useExecutionEvidence(
  instanceId: string,
  fetchPage: PageFetcher = getRuntimeEvents,
): EvState {
  const [state, setState] = useState<EvState>({ kind: 'loading' });

  // Render-phase reset on instance switch (no stale prior-instance frame committed).
  const [tracked, setTracked] = useState(instanceId);
  if (instanceId !== tracked) {
    setTracked(instanceId);
    setState({ kind: 'loading' });
  }

  useEffect(() => {
    let cancelled = false;
    let stopped = false;
    const byId = new Map<number, RuntimeEventRecord>();
    let since = 0;

    // Page forward from the cursor to "now": accumulate by id, advance `since` to max(id), stop when a
    // batch is shorter than the page (caught up). A short page is NOT terminal — the poll re-runs.
    const catchUp = async (): Promise<void> => {
      for (;;) {
        const batch = await fetchPage(instanceId, since, PAGE);
        if (cancelled) return;
        for (const e of batch) byId.set(e.id, e);
        if (batch.length > 0) since = batch.reduce((m, e) => (e.id > m ? e.id : m), since);
        if (batch.length < PAGE) return;
      }
    };

    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async (): Promise<void> => {
      try {
        await catchUp();
        if (cancelled) return;
        const events = [...byId.values()].sort((a, b) => a.id - b.id);
        const terminal = isRunTerminal(events);
        // Show data as soon as there is any, OR once the run is terminal (an empty terminal run is a
        // real "no legs" result). While non-terminal with nothing yet, stay `loading` — never "no legs".
        if (terminal || events.length > 0) {
          setState({ kind: 'ready', evidence: projectExecutionEvidence(events) });
        }
        if (terminal) stopped = true;
      } catch (err) {
        // Guard on `stopped` too: a late failure from an older/stale attempt must never overwrite a
        // newer terminal-ready result.
        if (cancelled || stopped) return;
        stopped = true;
        setState({ kind: 'error', status: err instanceof ApiError ? err.status : undefined });
      } finally {
        // Single-flight: schedule the NEXT poll only AFTER this one settles (recursive setTimeout, not
        // a fixed interval) — so a slow request can never overlap the next tick on the shared cursor.
        if (!cancelled && !stopped) timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };

    void tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [instanceId, fetchPage]);

  return state;
}

function CountCell({ label, value }: { label: string; value: number }) {
  return (
    <div className={styles.execCell}>
      <span className={styles.execCount}>{value}</span>
      <span className={styles.execCellLabel}>{label}</span>
    </div>
  );
}

export function ExecutionEvidenceSection({
  instanceId,
  useEvidence = useExecutionEvidence,
}: {
  instanceId: string;
  useEvidence?: (id: string) => EvState;
}) {
  const state = useEvidence(instanceId);

  return (
    <section className={styles.panel} aria-label="Execution evidence" data-testid="execution-evidence">
      <h2 className={styles.h2}>Execution evidence</h2>

      {state.kind === 'loading' && (
        <p className={styles.note} data-testid="exec-loading">Recording execution evidence…</p>
      )}

      {state.kind === 'error' && (
        <p className={styles.note} role="alert" data-testid="exec-error">
          Couldn&apos;t load execution evidence for this run. Check your session and try again.
        </p>
      )}

      {state.kind === 'ready' && state.evidence.decisionsObserved === 0 && (
        <p className={styles.note} data-testid="exec-empty">No execution legs were recorded for this run.</p>
      )}

      {state.kind === 'ready' && state.evidence.decisionsObserved > 0 && (
        <Ready evidence={state.evidence} />
      )}
    </section>
  );
}

function Ready({ evidence }: { evidence: ExecutionEvidence }) {
  const shown = evidence.receipts.slice(0, RECEIPT_CAP);
  const attempted = evidence.attemptedLegTotal > 0;
  return (
    <>
      <div className={styles.execSummary} data-testid="exec-summary">
        <CountCell label="Decisions observed" value={evidence.decisionsObserved} />
        {Object.entries(evidence.byKind).map(([kind, n]) => (
          <CountCell key={kind} label={kind} value={n} />
        ))}
        <CountCell label="Decisions with ≥1 attempted leg" value={evidence.decisionsWithAttemptedLegs} />
        <CountCell label="Attempted legs (total)" value={evidence.attemptedLegTotal} />
      </div>

      {/* Headline is conditional on WHETHER a leg was attempted — an all-NO_QUOTE/HOLD run must not
          claim an attempted leg the rows below contradict. */}
      <p className={styles.execHonest} data-testid="exec-headline">
        {attempted ? `${RECEIPT_HEADLINE}. ${RECEIPT_SUPPORTING}` : NO_ATTEMPTED_HEADLINE}
      </p>

      <ol className={styles.execList} data-testid="exec-receipts">
        {shown.map((r) => (
          <li key={r.id} className={styles.execRow}>
            <span className={`${styles.execKind} mono`}>{r.decision_kind}</span>
            {r.legs.length === 0 ? (
              <span className={styles.execNoLeg}>{NO_LEG_COPY}</span>
            ) : (
              <span className={styles.execBadges}>
                {r.legs.map((l, i) => (
                  <span key={i} className={styles.execLeg}>
                    <span className={`${styles.execBadge} mono`}>{l.kind}</span>
                    {l.attempted && <span className={`${styles.execBadge} mono`}>ATTEMPTED</span>}
                    <span className={`${styles.execBadge} mono`}>ADMISSION: {l.admission}</span>
                    <span className={`${styles.execBadge} mono`}>EXECUTION: {l.execution_status}</span>
                    {l.frozen && <span className={`${styles.execBadge} mono`}>FROZEN</span>}
                    {l.possibly_unresolved && <span className={`${styles.execBadge} mono`}>POSSIBLY_UNRESOLVED</span>}
                  </span>
                ))}
              </span>
            )}
          </li>
        ))}
      </ol>

      {evidence.receipts.length > RECEIPT_CAP && (
        <p className={styles.note} data-testid="exec-truncation">
          Showing the first {RECEIPT_CAP} of {evidence.decisionsObserved} decisions — the summary above reflects all of them.
        </p>
      )}
    </>
  );
}
