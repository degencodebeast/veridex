import type { ProofTraceItem } from '@/lib/contracts';
import styles from './ProofTraceStrip.module.css';

const STATE_CLASS: Record<ProofTraceItem['state'], string> = {
  done: styles.done, active: styles.active, pending: styles.pending, not_applicable: styles.na,
};

export function ProofTraceStrip({ trace }: { trace: ProofTraceItem[] }) {
  return (
    <section className={styles.strip} aria-label="Proof trace">
      <ol className={styles.row}>
        {trace.map((item, i) => (
          <li key={item.stage} className={styles.item}>
            <span className={`${styles.dot} ${STATE_CLASS[item.state]}`} aria-hidden />
            <span className={styles.label}>{item.label}</span>
            {item.stage === 'receipt' ? <span className={styles.hint}>off-chain venue artifact</span> : null}
            {i < trace.length - 1 ? <span className={styles.arrow} aria-hidden>→</span> : null}
          </li>
        ))}
      </ol>
      <p className={styles.caption}>Live projection of the canonical log, not a separate source of truth.</p>
    </section>
  );
}
