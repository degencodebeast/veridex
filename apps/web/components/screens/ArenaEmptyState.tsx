import Link from 'next/link';
import styles from './ArenaEmptyState.module.css';

export function ArenaEmptyState() {
  return (
    <section className={styles.empty} aria-label="Arena — no live competition">
      <h1 className={styles.title}>Arena</h1>
      <p className={styles.lead}>No live competition right now.</p>
      <p className={styles.note}>
        Competitions are usually scheduled. Browse what is upcoming, or read a recently settled run&apos;s proof.
      </p>
      <div className={styles.actions}>
        <Link href="/competitions" className={styles.cta}>View upcoming competitions →</Link>
        <Link href="/leaderboard" className={styles.ctaSecondary}>Recent settled proofs (Leaderboard) →</Link>
      </div>
    </section>
  );
}
