'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { GLOSSARY } from '@/lib/glossary';
import { useLane, type Lane } from '@/hooks/useLane';
import { useMakerArenaResult } from '@/hooks/useMakerArenaResult';
import type { AgentSummary } from '@/lib/catalog';
import type { MakerArenaResultView, MakerLeaderboardRow } from '@/lib/contracts';
import styles from './AgentsScreen.module.css';

type Sort = 'clv' | 'runs';

export function AgentsScreen({
  // The DIRECTIONAL roster is supplied by the page, mock-gated (mock ON → the labeled DEMO AGENTS
  // fixture; mock OFF → honest-empty []). No fixture DEFAULT here — an absent `agents` renders an
  // honest-empty directional table, never a fabricated roster (T-2 fixture prohibition).
  agents = [],
  // Maker result is page-sourced via useMakerArenaResult (F-9). No sealed-fixture default here —
  // an absent `makerResult` triggers the honest live-fetch/honest-empty maker path, never a fixture.
  makerResult,
}: {
  agents?: AgentSummary[];
  makerResult?: MakerArenaResultView;
}) {
  const [lane, setLane] = useLane();
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<Sort>('clv');
  const makerState = useMakerArenaResult(lane === 'maker', makerResult);

  const shown = useMemo(() => {
    const filtered = agents.filter((a) => a.agent_name.toLowerCase().includes(q.toLowerCase()));
    // Null perf (an unscored /agents/roster row) sorts to the bottom — never coerced to 0 (a real 0
    // would be a fabricated break-even claim that outranks the honest "—" rows).
    const rank = (v: number | null) => v ?? Number.NEGATIVE_INFINITY;
    return [...filtered].sort((a, b) =>
      sort === 'clv' ? rank(b.avg_clv_bps) - rank(a.avg_clv_bps) : rank(b.runs) - rank(a.runs),
    );
  }, [agents, q, sort]);

  return (
    <section className={styles.screen} aria-label="Agents Directory">
      <header className={styles.head}>
        <h1 className={styles.title}>Agents</h1>
        <div className={styles.actions}>
          <Link href="/duel" className={styles.secondary}>⚔ Compare Two →</Link>
          <Link href="/studio" className={styles.primary}>+ Create Agent</Link>
        </div>
      </header>

      {/* LANE SWITCH — separate lanes, different agents; never one set re-ranked. */}
      <div className={styles.laneRow}>
        <span className={styles.laneLabel}>LANE</span>
        <SegmentedControl<Lane>
          ariaLabel="Agents lane"
          value={lane}
          onChange={setLane}
          options={[{ value: 'directional', label: 'Directional' }, { value: 'maker', label: 'Maker' }]}
        />
        <span className={styles.laneNote}>Separate lanes, different agents — not one set re-ranked.</span>
      </div>

      {lane === 'directional' ? (
        <>
          <div className={styles.controls}>
            <input
              type="search" role="searchbox" className={styles.search} placeholder="Search agents…"
              value={q} onChange={(e) => setQ(e.target.value)}
            />
            <SegmentedControl<Sort>
              ariaLabel="Sort" value={sort} onChange={setSort}
              options={[{ value: 'clv', label: 'Avg CLV' }, { value: 'runs', label: 'Runs' }]}
            />
          </div>

          {shown.length === 0 ? (
            // Honest-empty when the roster itself is empty (off-mock, no agents-list backend) vs. a
            // filtered-out search over a non-empty roster — never a fabricated fallback either way.
            <p className={styles.empty} data-testid="agents-empty">
              {agents.length === 0 ? 'No agents yet.' : 'No agents match.'}
            </p>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr><th>AGENT</th><th>ARCHETYPE</th><th>MODE</th><th className={styles.r}>AVG CLV</th><th className={styles.r}>RUNS</th><th>PROOF</th><th>SOURCE</th></tr>
                </thead>
                <tbody>
                  {shown.map((a) => (
                    <tr key={a.agent_id} className={styles.row}>
                      <td><Link href={`/agents/${a.agent_id}`} className={styles.link}>{a.agent_name} ›</Link></td>
                      <td className="mono">{a.archetype}</td>
                      <td className="mono">{a.mode ?? '—'}</td>
                      <td className={styles.num}><Num value={a.avg_clv_bps} kind="bps" /></td>
                      <td className={styles.num}>{a.runs ?? '—'}</td>
                      <td><Badge variant={a.proof_mode} /></td>
                      <td>
                        {a.source_mode === 'live' ? <Badge variant="live" />
                          : a.source_mode === 'replay' ? <Badge variant="replay" />
                            : <span className={`${styles.mixedSrc} mono`}>mixed</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        makerState.status === 'loading' || makerState.status === 'idle' ? (
          <p className={styles.empty} data-testid="maker-loading" aria-live="polite">Loading maker result…</p>
        ) : makerState.status === 'unavailable' ? (
          <p className={styles.empty} data-testid="maker-unavailable" role="alert">Maker data unavailable.</p>
        ) : makerState.result.leaderboard.length === 0 ? (
          <p className={styles.empty} data-testid="maker-empty">No maker results available.</p>
        ) : (
          <MakerAgentsTable result={makerState.result} />
        )
      )}
    </section>
  );
}

// Maker Arena lane (MM-R1) — the 2 maker agents only (txline-fair-mm = candidate, naive-mm =
// control), a SEPARATE population from the directional roster (SEC-005). Ranked by toxicity
// loss ASC, never CLV — no Avg CLV column here. Rows deep-link to the Maker Proof Card.
function MakerAgentsTable({ result }: { result: MakerArenaResultView }) {
  const ranked = useMemo(
    () => [...result.leaderboard].sort((a, b) => a.avg_toxicity_loss_bps - b.avg_toxicity_loss_bps),
    [result],
  );

  return (
    <>
      <div className={styles.controls}>
        <span className={styles.makerSortNote}>ranked by adverse-selection toxicity (lower = better) · <Badge variant="mm-r1" /> · n={result.fixture_universe_n}</span>
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>MAKER AGENT</th><th>ROLE</th>
              <th className={styles.r}>TOXICITY LOSS ↓ <InfoTip label={GLOSSARY.toxicity_loss.label}>{GLOSSARY.toxicity_loss.definition}</InfoTip></th>
              <th className={styles.r}>MARKOUT <InfoTip label={GLOSSARY.mean_markout_diagnostic.label}>{GLOSSARY.mean_markout_diagnostic.definition}</InfoTip></th>
              <th className={styles.r}>QUOTES</th>
              <th>RUNG</th><th></th>
            </tr>
          </thead>
          <tbody>
            {ranked.map((r: MakerLeaderboardRow) => {
              const meta = MAKER_AGENT_META[r.agent_id];
              return (
                <tr key={r.agent_id} className={styles.row} data-testid="maker-agent-row">
                  <td><Link href={`/proof/maker/${r.agent_id}`} className={styles.link} data-testid="maker-agent-link">{r.agent_id} ›</Link></td>
                  <td className="mono">{meta?.role ?? '—'}</td>
                  <td className={styles.num}><Num value={r.avg_toxicity_loss_bps} kind="bps" /></td>
                  <td className={`${styles.num} ${styles.diagnostic}`}>{r.avg_markout_bps}</td>
                  <td className={styles.num}>{r.quote_count.toLocaleString()}</td>
                  <td><Badge variant="mm-r1" /></td>
                  {/* A real, keyboard-reachable link — never dead arrow text (I-R Min5). */}
                  <td className={styles.proofLink}>
                    <Link href={`/proof/maker/${r.agent_id}`} className={styles.proofLinkAnchor} aria-label={`Proof card for ${r.agent_id}`}>
                      PROOF →
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className={styles.tableFoot}>
          <span className={styles.footNote}>
            <b>ⓘ markout is diagnostic only</b>, never CLV — naive-mm&apos;s higher markout is more toxic → ranked 2nd. No fill / PnL / executable-edge claim (null by construction). Maker agents never appear in the CLV-ranked list.
          </span>
          <span className={styles.footCount}>{ranked.length} maker agents · n={result.fixture_universe_n}</span>
        </div>
      </div>
    </>
  );
}
