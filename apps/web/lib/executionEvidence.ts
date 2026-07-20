// Pure projection of a deployed instance's durable OPS stream into "Execution evidence" — the
// judge-facing action -> attempted-leg -> honest-dry-run-receipt chain (Step-2 brief). It reads the
// backend's `leg_receipt` tool_call events VERBATIM: no field is reinterpreted, no enum relabeled, and
// action_emitted rows are NEVER index-joined to tool_call rows (there is no shared correlation id).
// Behavior/telemetry only (SEC-003): never a rank, fill, PnL, or profit claim.
import type { RuntimeEventRecord } from '@/lib/api';

// Honest labels — a machine enum shown alone (e.g. "ABSTAINED") reads as "the strategy did nothing",
// so we bind it to its dry-run context. Enums are rendered verbatim; only the framing copy is ours.
export const RECEIPT_HEADLINE = 'ATTEMPTED DRY-RUN LEG · NOT SUBMITTED TO A VENUE';
// Shown when the run produced decisions but attempted NO execution leg (all NO_QUOTE/HOLD) — so the
// section never claims an attempted leg the rows contradict.
export const NO_ATTEMPTED_HEADLINE = 'NO EXECUTION LEG ATTEMPTED · NOTHING SUBMITTED TO A VENUE';
export const RECEIPT_SUPPORTING =
  'The offline dry-run proposer recorded this approved attempt. No order was submitted to a venue and no live money moved.';
export const NO_LEG_COPY = 'No execution leg was produced.';

export interface ProjectedLeg {
  kind: string;
  attempted: boolean;
  admission: string;
  execution_status: string;
  frozen: boolean;
  possibly_unresolved: boolean;
}

export interface ReceiptRow {
  id: number; // durable BIGSERIAL cursor id (also the stable render order key)
  decision_kind: string;
  legs: ProjectedLeg[];
}

export interface ExecutionEvidence {
  decisionsObserved: number; // # of leg_receipt events (one per decision)
  byKind: Record<string, number>; // decision_kind -> count (e.g. QUOTE_TWO_SIDED/NO_QUOTE/HOLD)
  // Decisions carrying >= 1 attempted leg — NOT filtered by decision_kind (a future cancel/replace leg
  // could ride a non-quote decision), so named for what it counts, not the kind we expect today.
  decisionsWithAttemptedLegs: number;
  attemptedLegTotal: number; // TRUE total attempted legs (a two-sided quote carries 2) — computed, not # decisions
  receipts: ReceiptRow[]; // leg_receipt events in durable id order
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}
function bool(v: unknown): boolean {
  return v === true;
}

function isLegReceipt(e: RuntimeEventRecord): boolean {
  return e.type === 'tool_call' && (e.payload as Record<string, unknown>)?.telemetry === 'leg_receipt';
}

function toLeg(raw: unknown): ProjectedLeg {
  const l = (raw ?? {}) as Record<string, unknown>;
  return {
    kind: str(l.kind),
    attempted: bool(l.attempted),
    admission: str(l.admission),
    execution_status: str(l.execution_status),
    frozen: bool(l.frozen),
    possibly_unresolved: bool(l.possibly_unresolved),
  };
}

function toReceipt(e: RuntimeEventRecord): ReceiptRow {
  const p = e.payload as Record<string, unknown>;
  const legs = Array.isArray(p.legs) ? p.legs.map(toLeg) : [];
  return { id: e.id, decision_kind: str(p.decision_kind), legs };
}

/**
 * Project the durable OPS events into the Execution-evidence summary + receipt list.
 *
 * Receipts are the `leg_receipt` tool_call events in durable `id` order (never re-sorted by the
 * non-deterministic `ts`). `attemptedLegTotal` sums `legs.filter(attempted)` — it is NOT the count of
 * quote decisions, because a two-sided quote carries two legs.
 */
export function projectExecutionEvidence(events: readonly RuntimeEventRecord[]): ExecutionEvidence {
  const receipts = events
    .filter(isLegReceipt)
    .map(toReceipt)
    .sort((a, b) => a.id - b.id);

  const byKind: Record<string, number> = {};
  let decisionsWithAttemptedLegs = 0;
  let attemptedLegTotal = 0;
  for (const r of receipts) {
    byKind[r.decision_kind] = (byKind[r.decision_kind] ?? 0) + 1;
    const attempted = r.legs.filter((l) => l.attempted);
    if (attempted.length > 0) decisionsWithAttemptedLegs += 1;
    attemptedLegTotal += attempted.length;
  }

  return {
    decisionsObserved: receipts.length,
    byKind,
    decisionsWithAttemptedLegs,
    attemptedLegTotal,
    receipts,
  };
}
