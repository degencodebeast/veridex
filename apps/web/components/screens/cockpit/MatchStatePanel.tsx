import type { MatchState } from '@/lib/contracts';
import styles from './MatchStatePanel.module.css';

// REQ-040: render the confirmed soccer stat set ONLY. There is intentionally no
// `possession` field on MatchState, so it cannot be rendered (AC-012).
//
// B/C framing: goals/cards/corners/phase come from the TxLINE soccer scores feed (stat-keys 1-8 +
// the 19-value phase enum) — REAL upstream but Veridex stubs scores={} today (txline_normalize.py:117),
// so LIVE is honest-empty ("pending scores-feed") = (B) wireable. The running minute/clock is (C)
// genuinely absent — TxLINE tracks WHICH HALF, not elapsed time; never imply a live clock.
export function MatchStatePanel({ match }: { match: MatchState }) {
  // The live emptyMatch signature (scores not wired): scheduled, no clock, 0-0. Demo/real has data.
  const noCoverage = match.status === 'scheduled' && match.minute === null
    && match.goals[0] === 0 && match.goals[1] === 0;

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
        <span className={`${styles.status} mono`}>{noCoverage ? 'no feed' : match.status}</span>
      </div>
      {noCoverage ? (
        // Honest-empty: real upstream, pending the TxLINE scores-feed normalizer. NO fabricated
        // 0-0 stats, NO live minute (TxLINE has no elapsed-minute source — phase only).
        <p className={styles.empty} data-testid="match-empty">
          Live match stats pending — goals, cards, corners &amp; phase come from the TxLINE scores feed
          (wireable, not yet normalized). TxLINE has no elapsed-minute source, only the match phase.
        </p>
      ) : (
        <>
          <div className={styles.scoreRow}>
            <span className={`${styles.score} mono`}>{match.goals[0]} – {match.goals[1]}</span>
            <span className={`${styles.phase} mono`}>
              {match.phase}{match.minute != null ? ` ${match.minute}'` : ''}
            </span>
          </div>
          <div className={styles.stats} data-testid="match-stats">
            {stat('goals', match.goals[0], match.goals[1])}
            {stat('yellow', match.yellow[0], match.yellow[1])}
            {stat('red', match.red[0], match.red[1])}
            {stat('corners', match.corners[0], match.corners[1])}
          </div>
          <p className={styles.note}>Cards &amp; corners are match stats, not tradable markets.</p>
        </>
      )}
    </section>
  );
}
