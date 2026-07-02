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
        {decisions.map((d) => {
          // DISPLAY GATE (REQ-2D-501): edge_bps is the executable edge AT the venue price — it
          // renders ONLY when a REAL venue quote backs it (fail-closed). The min-edge THRESHOLD is
          // a config value and always renders. A Fake/paper quote never surfaces edge_bps as edge.
          const parts: string[] = [];
          if (d.real_venue_quote === true && d.edge_bps != null) parts.push(`edge ${d.edge_bps}`);
          if (d.min_edge_bps != null) parts.push(`min ${d.min_edge_bps} bps`);
          return (
            <li key={d.tick_seq} className={styles.row}>
              <span className={`${styles.decision} ${DECISION_CLASS[d.decision]} mono`}>{d.decision}</span>
              <span className={styles.reason}>{d.reason}</span>
              {parts.length ? <span className={`${styles.edge} mono`}>{parts.join(' · ')}</span> : <span />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
