'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { LiveDot } from '@/components/ui/LiveDot';
import { COMPETITIONS } from '@/lib/fixtures/catalog';
import type { CompetitionSummary, CompetitionType } from '@/lib/catalog';
import styles from './CompetitionsScreen.module.css';

const TYPE_LABEL: Record<CompetitionType, string> = {
  live_arena: 'Live', replay_arena: 'Replay', head_to_head: 'Head-to-Head', prize_vault_challenge: 'Prize-Vault',
};

function CtaFor({ c }: { c: CompetitionSummary }) {
  if (c.lifecycle === 'live') return <Link href={`/arena/${c.competition_id}`} className={styles.cta}>Enter Arena →</Link>;
  // Join routes to the competition's own arena/detail page (not the create flow — joining ≠ creating).
  if (c.lifecycle === 'upcoming') return <Link href={`/arena/${c.competition_id}`} className={styles.ctaSecondary}>Join</Link>;
  return <Link href={`/proof/${c.settled_run_id}`} className={styles.ctaSecondary}>Proof →</Link>;
}

function Group({ title, comps }: { title: string; comps: CompetitionSummary[] }) {
  if (comps.length === 0) return null;
  return (
    <section className={styles.group}>
      <h2 className={styles.h2}>{title}</h2>
      <div className={styles.rows}>
        {comps.map((c) => (
          <div key={c.competition_id} className={styles.row} data-testid={`comp-${c.competition_id}`}>
            <div className={styles.rowHead}>
              {c.lifecycle === 'live' && <LiveDot size={6} />}
              <span className={styles.compTitle}>{c.title}</span>
              <Badge variant={c.proof_mode} />
            </div>
            <div className={styles.pins}>
              <span className={styles.pin}>TYPE {TYPE_LABEL[c.competition_type]}</span>
              <span className={styles.pin}>SRC {c.source_mode}</span>
              <span className={styles.pin}>EXEC {c.execution_mode}</span>
              <span className={styles.pin}>ROSTER {c.roster_size}</span>
              <span className={styles.pin}>SCOPE {c.market_scope}</span>
            </div>
            <div className={styles.rowFoot}>
              <span className={styles.tiles}>
                <span className={`${styles.tile} mono`}>EVENTS/MIN {c.events_per_min ?? '—'}</span>
                <span className={`${styles.tile} mono`}>WS LIVE {c.ws_live ? 'yes' : 'no'}</span>
              </span>
              <CtaFor c={c} />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function CompetitionsScreen({ comps = COMPETITIONS }: { comps?: CompetitionSummary[] }) {
  const live = comps.filter((c) => c.lifecycle === 'live');
  const upcoming = comps.filter((c) => c.lifecycle === 'upcoming');
  const settled = comps.filter((c) => c.lifecycle === 'settled');
  return (
    <section className={styles.screen} aria-label="Competitions">
      <h1 className={styles.title}>Competitions</h1>
      <div className={styles.recent} data-testid="recent-settled">
        <span className={styles.recentLabel}>RECENT SETTLED</span>
        {settled.map((c) => (
          <Link key={c.competition_id} href={`/proof/${c.settled_run_id}`} className={styles.recentLink}>
            {c.title} ›
          </Link>
        ))}
      </div>
      <Group title="Live" comps={live} />
      <Group title="Upcoming" comps={upcoming} />
      <Group title="Completed" comps={settled} />
    </section>
  );
}
