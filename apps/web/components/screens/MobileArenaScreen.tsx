'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { rankByAvgClv } from '@/lib/derive';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import type { LeaderboardRow } from '@/lib/catalog';
import styles from './MobileArenaScreen.module.css';

const TABS = [
  { label: 'Arena', href: '/m/arena' }, { label: 'Agents', href: '/agents' },
  { label: 'Proof', href: '/leaderboard' }, { label: 'Rank', href: '/leaderboard' },
];

export function MobileArenaScreen({ rows = LEADERBOARD_ROWS }: { rows?: LeaderboardRow[] }) {
  const ranked = rankByAvgClv(rows);
  return (
    <div className={styles.frame} data-testid="phone-frame" data-width="392">
      <header className={styles.appbar}>
        <span className={styles.logo}>V</span>
        {/* Static demo shell — not a live feed. Honestly labelled (no live/SCORING affordance). */}
        <span className={styles.scoring}>DEMO · REPLAY</span>
      </header>

      <div className={styles.toggle} data-testid="mobile-match">
        <Badge variant="replay" /> <span className={styles.toggleText}>FRA v BRA · H2 62&apos; · mock data</span>
      </div>

      <div className={styles.fixtureCard}>
        <div className={styles.tile}><span className={styles.tileLabel}>SCORE</span><span className="mono">1 - 1</span></div>
        <div className={styles.tile}><span className={styles.tileLabel}>PHASE</span><span className="mono">H2</span></div>
        <div className={styles.tile}><span className={styles.tileLabel}>EVENTS/MIN</span><span className="mono">11</span></div>
      </div>

      <div className={styles.cards}>
        {ranked.map((r) => (
          <div key={r.agent_id} className={styles.card} data-testid="mobile-lb-card">
            <div className={styles.cardTop}>
              <span className={styles.rank} data-testid="mobile-rank">{r.rank}</span>
              <span className={styles.name}>{r.agent_name}</span>
              <span className={styles.bigClv}><Num value={r.avg_clv_bps} kind="bps" /></span>
            </div>
            <div className={styles.cardBadges}>
              <Badge variant={r.proof_mode} />
              {r.source_mode === 'live' ? <Badge variant="live" />
                : r.source_mode === 'replay' ? <Badge variant="replay" />
                  : <span className={`${styles.mixedSrc} mono`}>mixed</span>}
            </div>
          </div>
        ))}
      </div>

      <nav className={styles.tabs} data-testid="bottom-tabs" aria-label="Mobile tabs">
        {TABS.map((t) => (
          <Link key={t.label} href={t.href} className={styles.tab}>{t.label}</Link>
        ))}
      </nav>
    </div>
  );
}
