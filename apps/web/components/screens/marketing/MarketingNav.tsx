import Link from 'next/link';
import styles from './MarketingNav.module.css';

// Standalone sticky nav shared by the two public explainer pages (How It Works · Why Veridex).
// These pages live OUTSIDE the (app) route group, so they carry their own chrome — the global
// wordmark, cross-links between the two long-form pages + Home, and the Enter App entry — and
// never the AppShell/left rail. `active` marks the current page as non-interactive present text.
export function MarketingNav({ active }: { active: 'how' | 'why' }) {
  return (
    <nav className={styles.nav} aria-label="Explainer">
      <Link href="/" className={styles.brandLink} aria-label="Veridex home">
        <span className={styles.logo} aria-hidden>V</span>
        <span className={styles.brand}>VERIDEX</span>
      </Link>
      <span className={styles.spacer} />
      <div className={styles.links}>
        {active === 'how' ? (
          <span className={styles.current} aria-current="page">How it works</span>
        ) : (
          <Link href="/how-it-works" className={styles.link}>How it works</Link>
        )}
        {active === 'why' ? (
          <span className={styles.current} aria-current="page">Why Veridex</span>
        ) : (
          <Link href="/why-veridex" className={styles.link}>Why Veridex</Link>
        )}
        <Link href="/" className={styles.link}>Home</Link>
      </div>
      <Link href="/competitions" className={styles.enter}>Enter App →</Link>
    </nav>
  );
}
