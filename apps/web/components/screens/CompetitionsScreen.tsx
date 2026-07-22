'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { LiveDot } from '@/components/ui/LiveDot';
import { isMockEnabled } from '@/lib/mock';
import type { CompetitionRecordView } from '@/lib/api';
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

// No fixture DEFAULT props (T-2): absent `comps`/`rewards` render an honest-empty screen, never the
// COMPETITIONS / MY_REWARDS fixtures. The owning page injects fixtures ONLY under the mock gate; off
// the gate it passes `[]`, so nothing fabricated can leak onto the "Enter App" landing tab.
export function CompetitionsScreen({ comps = [], rewards = [], records }: {
  comps?: CompetitionSummary[]; rewards?: RewardSummary[];
  // Off-mock REAL competition records (GET /competitions, spec §6.1). `undefined` = mock path (the
  // fixture `comps`/`rewards` render instead); an empty array = off-mock honest empty state.
  records?: CompetitionRecordView[];
}) {
  // Mock-gate (hydration-safe: default off on SSR/first render, then read after mount). Roadmappable
  // demo fields (LEADER CLV) populate ONLY under mock; live shows honest "—" until backend-wired.
  const [mock, setMock] = useState(false);
  useEffect(() => { setMock(isMockEnabled()); }, []);
  const live = comps.filter((c) => c.lifecycle === 'live');
  const upcoming = comps.filter((c) => c.lifecycle === 'upcoming');
  const settled = comps.filter((c) => c.lifecycle === 'settled');
  const leaderClv = (c: CompetitionSummary) =>
    mock && c.demo_leader_clv_bps != null ? `${c.demo_leader_clv_bps >= 0 ? '+' : ''}${c.demo_leader_clv_bps.toFixed(1)} bps` : null;
  // PRIZE is the honest design-target label from the rewards join (Prize Vault doctrine: designed,
  // Phase 2D, no funds move). No reward entry ⇒ "No vault" (clearer than a bare —). The column is
  // fenced by a header disclaimer so V5's literal "PRIZE" can never read as a funded/held pool.
  const prizeFor = (id: string) => rewards.find((r) => r.competition_id === id)?.amount_label ?? 'No vault';

  return (
    <section className={styles.screen} aria-label="Competitions">
      <h1 className={styles.title}>Competitions</h1>

      {records !== undefined && (
        <section className={styles.group} data-testid="real-competitions" aria-label="Your competitions">
          <div className={styles.h2Row}>
            <h2 className={styles.h2}>Competitions</h2>
            {/* Coherent count DERIVED from the real records — never the empty mock aggregate. */}
            <span className={styles.count} data-testid="real-total">{records.length}</span>
          </div>
          {records.length === 0 ? (
            <div data-testid="competitions-empty">
              <p>No competitions yet.</p>
              <div>
                <Link href="/competitions/create" className={styles.cta}>Create a competition →</Link>
                <Link href="/markets" className={styles.ctaSecondary}>Browse the Replay Library (Markets) →</Link>
              </div>
            </div>
          ) : (
            <table className={styles.table} data-testid="real-competitions-table">
              <thead>
                <tr><th scope="col">COMPETITION</th><th scope="col">STATUS</th><th scope="col">SOURCE</th><th scope="col">EXEC</th><th scope="col">AGENTS</th></tr>
              </thead>
              <tbody>
                {records.map((r) => (
                  <tr key={r.competitionId} data-testid={`record-${r.competitionId}`}>
                    <td><Link href={`/arena/${r.competitionId}`} className={styles.recentLink}>{r.title} ›</Link></td>
                    <td className="mono" data-testid={`record-status-${r.competitionId}`}>{r.status}</td>
                    {/* absent server fields render "—", NEVER a fabricated replay/paper/roster value */}
                    <td className="mono">{r.sourceMode ?? '—'}</td>
                    <td className="mono">{r.executionMode ?? '—'}</td>
                    <td className="mono">{r.rosterSize ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {/* MOCK-PATH surfaces only (records === undefined). Off-mock the real-records section above is the
          single source of truth, so these never render alongside a real record → no TOTAL 0 / "No
          competitions to show." contradiction (finding 6.2). */}
      {records === undefined && (
       <>
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
            {settled.length === 0 ? (
              <tr>
                <td className={styles.muted} colSpan={3} data-testid="recent-settled-empty">
                  No settled competitions yet.
                </td>
              </tr>
            ) : settled.map((c) => (
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
              <th scope="col" className={styles.num}>AGENTS</th>
              <th scope="col" className={styles.num}>LEADER CLV</th>
              <th scope="col" title="Design target only · no funds held or paid">
                PRIZE<span className={styles.thCaption} data-testid="prize-caption">Design target only · no funds held or paid</span>
              </th>
              <th scope="col">PROOF</th>
              <th scope="col">STATUS</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {comps.length === 0 && (
              <tr>
                <td className={styles.muted} colSpan={10} data-testid="all-competitions-empty">
                  No competitions to show.
                </td>
              </tr>
            )}
            {comps.map((c) => (
              <tr key={c.competition_id} data-testid={`comp-${c.competition_id}`}>
                <td className={styles.compCell}>{c.lifecycle === 'live' && <LiveDot size={5} />}{c.title}</td>
                <td className="mono">{TYPE_LABEL[c.competition_type]}</td>
                <td data-testid="source-cell"><Badge variant={c.source_mode === 'live' ? 'live' : 'replay'} /></td>
                <td className="mono">{c.execution_mode}</td>
                {/* AGENTS = roster_size — real, trusted data (the LIVE-NOW card shows it too) */}
                <td className={`${styles.num} mono`} data-testid="agents-cell">{c.roster_size}</td>
                {/* no honest comp→leader source → honest — (never a fabricated leader) */}
                <td className={styles.num} data-testid="leader-cell">{leaderClv(c) ?? <span className={styles.muted}>—</span>}</td>
                {/* designed target, never moved funds */}
                <td className={`${styles.muted} mono`} data-testid="prize-cell">{prizeFor(c.competition_id)}</td>
                <td><Badge variant={c.proof_mode} /></td>
                <td className="mono" data-testid="status-cell">{c.lifecycle}</td>
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
       </>
      )}
    </section>
  );
}
