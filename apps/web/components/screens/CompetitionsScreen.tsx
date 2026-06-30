'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { LiveDot } from '@/components/ui/LiveDot';
import { COMPETITIONS, MY_REWARDS } from '@/lib/fixtures/catalog';
import type { CompetitionSummary, CompetitionType, RewardSummary } from '@/lib/catalog';
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

// Rich LIVE-NOW card (V5) — only for genuinely-live competitions.
function LiveCard({ c }: { c: CompetitionSummary }) {
  return (
    <div className={styles.row} data-testid={`live-${c.competition_id}`}>
      <div className={styles.rowHead}>
        <LiveDot size={6} />
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
  );
}

export function CompetitionsScreen({ comps = COMPETITIONS, rewards = MY_REWARDS }: {
  comps?: CompetitionSummary[]; rewards?: RewardSummary[];
}) {
  const live = comps.filter((c) => c.lifecycle === 'live');
  const upcoming = comps.filter((c) => c.lifecycle === 'upcoming');
  const settled = comps.filter((c) => c.lifecycle === 'settled');
  // PRIZE is the honest design-target label from the rewards join (Prize Vault doctrine: designed,
  // Phase 2D, no funds move). No reward entry ⇒ "—". Never a fabricated figure / custody claim.
  const prizeFor = (id: string) => rewards.find((r) => r.competition_id === id)?.amount_label ?? '—';

  return (
    <section className={styles.screen} aria-label="Competitions">
      <h1 className={styles.title}>Competitions</h1>

      {/* Stat cards — DERIVED counts from the real list, not a fabricated aggregate band. */}
      <div className={styles.statCards} data-testid="stat-cards">
        <div className={styles.statCard}><span className={styles.statNum} data-testid="stat-live">{live.length}</span><span className={styles.statLabel}>LIVE</span></div>
        <div className={styles.statCard}><span className={styles.statNum} data-testid="stat-upcoming">{upcoming.length}</span><span className={styles.statLabel}>UPCOMING</span></div>
        <div className={styles.statCard}><span className={styles.statNum} data-testid="stat-settled">{settled.length}</span><span className={styles.statLabel}>SETTLED</span></div>
        <div className={styles.statCard}><span className={styles.statNum} data-testid="stat-total">{comps.length}</span><span className={styles.statLabel}>TOTAL</span></div>
      </div>

      {live.length > 0 && (
        <section className={styles.group}>
          <h2 className={styles.h2}>Live now</h2>
          <div className={styles.rows}>{live.map((c) => <LiveCard key={c.competition_id} c={c} />)}</div>
        </section>
      )}

      {/* RECENT SETTLED — table linking each settled run to its Proof. */}
      <section className={styles.group}>
        <h2 className={styles.h2}>Recent settled</h2>
        <table className={styles.table} data-testid="recent-settled">
          <thead>
            <tr><th scope="col">COMPETITION</th><th scope="col">TYPE</th><th scope="col">PROOF</th></tr>
          </thead>
          <tbody>
            {settled.map((c) => (
              <tr key={c.competition_id}>
                <td><Link href={`/proof/${c.settled_run_id}`} className={styles.recentLink}>{c.title} ›</Link></td>
                <td className="mono">{TYPE_LABEL[c.competition_type]}</td>
                <td><Badge variant={c.proof_mode} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* ALL COMPETITIONS — the dense canonical list. SOURCE (source_mode) and STATUS (lifecycle)
          are SEPARATE axes; replay is labeled REPLAY, never LIVE. PRIZE = honest design-target;
          LEADER CLV = "—" (no honest comp→leader link — never a fabricated leader). */}
      <section className={styles.group}>
        <h2 className={styles.h2}>All competitions</h2>
        <table className={styles.table} data-testid="all-competitions">
          <thead>
            <tr>
              <th scope="col">COMPETITION</th>
              <th scope="col">TYPE</th>
              <th scope="col">SOURCE</th>
              <th scope="col">EXEC</th>
              <th scope="col">PROOF</th>
              <th scope="col">STATUS</th>
              <th scope="col">PRIZE</th>
              <th scope="col">LEADER CLV</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {comps.map((c) => (
              <tr key={c.competition_id} data-testid={`comp-${c.competition_id}`}>
                <td className={styles.compCell}>{c.lifecycle === 'live' && <LiveDot size={5} />}{c.title}</td>
                <td className="mono">{TYPE_LABEL[c.competition_type]}</td>
                <td data-testid="source-cell"><Badge variant={c.source_mode === 'live' ? 'live' : 'replay'} /></td>
                <td className="mono">{c.execution_mode}</td>
                <td><Badge variant={c.proof_mode} /></td>
                <td className="mono" data-testid="status-cell">{c.lifecycle}</td>
                {/* designed target, never moved funds */}
                <td className={`${styles.muted} mono`} data-testid="prize-cell">{prizeFor(c.competition_id)}</td>
                {/* no honest comp→leader source → honest — (never a fabricated leader) */}
                <td className={styles.num} data-testid="leader-cell"><span className={styles.muted}>—</span></td>
                <td className={styles.actionCell}><CtaFor c={c} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={styles.legend}>
          PRIZE is a designed target (Prize Vault · Phase 2D · no funds move). LEADER CLV shows — until a
          per-competition top-CLV mapping is wired — never a fabricated leader. Rank stays CLV-only.
        </p>
      </section>
    </section>
  );
}
