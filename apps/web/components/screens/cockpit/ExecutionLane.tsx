import type { ExecutionReceipt, ReceiptStatus } from '@/lib/contracts';
import styles from './ExecutionLane.module.css';

const LANE: ReceiptStatus[] = ['proposed', 'law_approved', 'policy_approved', 'submitted', 'filled'];
const TERMINAL_NEGATIVE: ReceiptStatus[] = ['rejected', 'cancelled'];
const LABEL: Record<ReceiptStatus, string> = {
  proposed: 'proposed', law_approved: 'law approved', policy_approved: 'policy approved',
  submitted: 'submitted', filled: 'filled', rejected: 'rejected', cancelled: 'cancelled',
};

// Furthest positive lifecycle stage the receipt provably reached. For a positive
// status that's its own index; for a terminal-negative (rejected/cancelled) status —
// which is NOT a lane position — infer from timestamps so a receipt that progressed
// then failed still shows the progress it made (never fabricates a stage).
function reachedIndex(r: ExecutionReceipt): number {
  if (LANE.includes(r.status)) return LANE.indexOf(r.status);
  if (r.submitted_at != null || r.settled_at != null) return LANE.indexOf('submitted');
  return LANE.indexOf('proposed');
}

export function ExecutionLane({ receipts }: { receipts: ExecutionReceipt[] }) {
  const latest = receipts[receipts.length - 1];
  const terminalNeg = latest ? TERMINAL_NEGATIVE.includes(latest.status) : false;
  const idx = latest ? reachedIndex(latest) : -1;
  return (
    <section className={styles.panel} aria-label="Execution lane">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>EXECUTION LANE</span>
        <span className={styles.fence}>off-chain venue artifact · non-scoring · not the Solana Memo anchor</span>
      </div>
      <ol className={styles.lane}>
        {LANE.map((stage, i) => {
          const on = idx >= LANE.indexOf(stage);
          const halted = terminalNeg && i === idx; // last reached stage before failure
          return (
            <li
              key={stage}
              data-stage={stage}
              data-reached={on ? 'true' : 'false'}
              className={`${styles.stage} ${on ? styles.on : ''} ${halted ? styles.halted : ''}`}
            >
              <span className={styles.dot} aria-hidden />
              <span className={styles.label}>{LABEL[stage]}</span>
              {i < LANE.length - 1 ? <span className={styles.arrow} aria-hidden>→</span> : null}
            </li>
          );
        })}
        {terminalNeg && latest ? (
          <li className={styles.terminal} data-stage={latest.status}>
            <span className={styles.terminalDot} aria-hidden />
            <span className={styles.terminalLabel}>{LABEL[latest.status]}</span>
          </li>
        ) : null}
      </ol>
      {latest ? (
        <div className={`${styles.receipt} mono`}>
          {latest.venue} · {latest.market_ref} · {latest.side} · {latest.filled_size}/{latest.requested_size} @ {latest.price} · {latest.mode}
        </div>
      ) : null}
    </section>
  );
}
