'use client';
import { useEffect, useState } from 'react';
import { isMockEnabled } from '@/lib/mock';
import styles from './MockBanner.module.css';

// Persistent honest indicator: whenever MOCK MODE is on, every app screen carries a visible
// "DEMO DATA · MOCK MODE" strip so fixtures are never mistaken for live data (doctrine). Mount-
// gated (effect) so the `?mock=1` per-tab path applies without an SSR hydration mismatch.
export function MockBanner() {
  const [on, setOn] = useState(false);
  useEffect(() => { setOn(isMockEnabled()); }, []);
  if (!on) return null;
  return (
    <div className={styles.banner} role="status" data-testid="mock-banner">
      <span className={styles.dot} aria-hidden />
      DEMO DATA · MOCK MODE — populated from fixtures, not a live backend (replay, never live)
    </div>
  );
}
