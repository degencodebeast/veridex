import type { MatchState } from '@/lib/contracts';
import styles from './MatchStatePanel.module.css';

// REQ-040: render the confirmed soccer stat set ONLY. There is intentionally no
// `possession` field on MatchState, so it cannot be rendered (AC-012).
export function MatchStatePanel({ match }: { match: MatchState }) {
  const stat = (label: string, h: number, a: number) => (
    <div className={styles.stat} key={label}>
      <span className={styles.statLabel}>{label}</span>
      <span className={`${styles.statVal} mono`}>{h}·{a}</span>
    </div>
  );
  return (
    <section className={styles.panel} aria-label="Match state">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>MATCH STATE</span>
        <span className={`${styles.status} mono`}>{match.status}</span>
      </div>
      <div className={styles.scoreRow}>
        <span className={`${styles.score} mono`}>{match.goals[0]} – {match.goals[1]}</span>
        <span className={`${styles.phase} mono`}>
          {match.phase}{match.minute != null ? ` ${match.minute}'` : ''}
        </span>
      </div>
      <div className={styles.stats}>
        {stat('goals', match.goals[0], match.goals[1])}
        {stat('yellow', match.yellow[0], match.yellow[1])}
        {stat('red', match.red[0], match.red[1])}
        {stat('corners', match.corners[0], match.corners[1])}
      </div>
      <p className={styles.note}>Cards &amp; corners are match stats, not tradable markets.</p>
    </section>
  );
}
