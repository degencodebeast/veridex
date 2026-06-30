import type { PolicyDecision } from '@/lib/contracts';
import styles from './PolicyDecisions.module.css';

const DECISION_CLASS: Record<PolicyDecision['decision'], string> = {
  ALLOW: styles.allow, DENY: styles.deny, REFUSE: styles.refuse,
};

export function PolicyDecisions({ decisions, killArmed }: { decisions: PolicyDecision[]; killArmed: boolean }) {
  return (
    <section className={styles.panel} aria-label="Policy decisions">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>POLICY DECISIONS</span>
        {killArmed ? <span className={`${styles.kill} mono`}>KILL ARMED</span> : null}
      </div>
      <ul className={styles.list}>
        {decisions.map((d) => (
          <li key={d.tick_seq} className={styles.row}>
            <span className={`${styles.decision} ${DECISION_CLASS[d.decision]} mono`}>{d.decision}</span>
            <span className={styles.reason}>{d.reason}</span>
            {d.edge_bps != null && d.min_edge_bps != null ? (
              <span className={`${styles.edge} mono`}>edge {d.edge_bps} · min {d.min_edge_bps} bps</span>
            ) : <span />}
          </li>
        ))}
      </ul>
    </section>
  );
}
