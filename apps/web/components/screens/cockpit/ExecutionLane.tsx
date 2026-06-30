import type { ExecutionReceipt, ReceiptStatus } from '@/lib/contracts';
import styles from './ExecutionLane.module.css';

const LANE: ReceiptStatus[] = ['proposed', 'law_approved', 'policy_approved', 'submitted', 'filled'];
const LABEL: Record<string, string> = {
  proposed: 'proposed', law_approved: 'law approved', policy_approved: 'policy approved',
  submitted: 'submitted', filled: 'filled',
};

function reached(status: ReceiptStatus, stage: ReceiptStatus): boolean {
  return LANE.indexOf(status) >= LANE.indexOf(stage);
}

export function ExecutionLane({ receipts }: { receipts: ExecutionReceipt[] }) {
  const latest = receipts[receipts.length - 1];
  return (
    <section className={styles.panel} aria-label="Execution lane">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>EXECUTION LANE</span>
        <span className={styles.fence}>off-chain venue artifact · non-scoring · not the Solana Memo anchor</span>
      </div>
      <ol className={styles.lane}>
        {LANE.map((stage, i) => {
          const on = latest ? reached(latest.status, stage) : false;
          return (
            <li key={stage} className={`${styles.stage} ${on ? styles.on : ''}`}>
              <span className={styles.dot} aria-hidden />
              <span className={styles.label}>{LABEL[stage]}</span>
              {i < LANE.length - 1 ? <span className={styles.arrow} aria-hidden>→</span> : null}
            </li>
          );
        })}
      </ol>
      {latest ? (
        <div className={`${styles.receipt} mono`}>
          {latest.venue} · {latest.market_ref} · {latest.side} · {latest.filled_size}/{latest.requested_size} @ {latest.price} · {latest.mode}
        </div>
      ) : null}
    </section>
  );
}
