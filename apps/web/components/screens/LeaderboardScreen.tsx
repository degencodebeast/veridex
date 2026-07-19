'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { ConfBar } from '@/components/ui/ConfBar';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { rankByAvgClv, isEligible } from '@/lib/derive';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT, MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { deriveMakerVerdict } from '@/lib/makerVerdict';
import { GLOSSARY } from '@/lib/glossary';
import { useLane, type Lane } from '@/hooks/useLane';
import type { LeaderboardRow } from '@/lib/catalog';
import type { MakerArenaResultView, MakerLeaderboardRow } from '@/lib/contracts';
import styles from './LeaderboardScreen.module.css';

type Filter = 'ALL' | 'REPLAY' | 'LIVE';

export function LeaderboardScreen({
  rows = LEADERBOARD_ROWS,
  makerResult = MAKER_ARENA_RESULT,
}: {
  rows?: LeaderboardRow[];
  makerResult?: MakerArenaResultView;
}) {
  const [lane, setLane] = useLane();
  const [filter, setFilter] = useState<Filter>('ALL');

  const ranked = useMemo(() => {
    const scoped = filter === 'ALL'
      ? rows
      : rows.filter((r) => r.source_mode === filter.toLowerCase());
    return rankByAvgClv(scoped); // sort key is ALWAYS avg_clv_bps (SEC-005)
  }, [rows, filter]);

  return (
    <section className={styles.screen} aria-label="Leaderboard">
      <header className={styles.head}>
        <h1 className={styles.title}>Leaderboard</h1>
      </header>

      {/* LANE SWITCH — a level above the source filter (a different measurement, not a
          filter on the same rows). URL-addressable via ?lane=maker. */}
      <div className={styles.laneRow}>
        <span className={styles.laneLabel}>LANE</span>
        <SegmentedControl<Lane>
          ariaLabel="Leaderboard lane"
          value={lane}
          onChange={setLane}
          options={[{ value: 'directional', label: 'Directional' }, { value: 'maker', label: 'Maker' }]}
        />
        <span className={styles.laneNote}>
          Directional ranks by <b>Avg CLV</b>; Maker ranks by <b>adverse-selection toxicity</b>. Separate lanes, different agents — not one set re-ranked.
        </span>
      </div>

      {lane === 'directional' ? (
        <>
          <div className={styles.filterRow}>
            <SegmentedControl<Filter>
              ariaLabel="Source filter"
              value={filter}
              onChange={setFilter}
              options={[{ value: 'ALL', label: 'ALL' }, { value: 'REPLAY', label: 'REPLAY' }, { value: 'LIVE', label: 'LIVE' }]}
            />
          </div>

          <p className={styles.banner}>
            Rank is Avg CLV only. Proof completeness gates eligibility, never rank. ⓟ Sim PnL &amp; Brier are simulated proxies — not settled profit.
          </p>

          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>#</th><th>AGENT</th><th className={styles.r}>RUNS</th>
                  <th className={styles.r}>AVG CLV</th><th className={styles.r}>TOTAL CLV</th>
                  <th className={styles.r}>SIM PNL ⓟ</th><th className={styles.r}>BRIER ⓟ</th>
                  <th className={styles.r}>MAX DD</th><th className={styles.r}>ACTIONS</th>
                  <th className={styles.r}>VALID</th><th>CONF</th>
                  <th>PROOF</th><th>ELIGIBILITY</th><th>ANCHOR</th><th>SOURCE</th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((r) => (
                  <tr key={r.agent_id} data-testid="lb-row" className={styles.row}>
                    <td className="mono" data-testid="lb-rank">{r.rank}</td>
                    <td data-testid="lb-agent">
                      <Link href={`/agents/${r.agent_id}`} className={styles.agentLink}>
                        {r.agent_name} <span className={styles.kind}>{r.agent_kind}</span> ›
                      </Link>
                    </td>
                    <td className={styles.num}>{r.runs}</td>
                    <td className={styles.num} data-testid="lb-clv">{r.avg_clv_bps === null ? '—' : <Num value={r.avg_clv_bps} kind="bps" />}</td>
                    <td className={styles.num}><Num value={r.total_clv_bps} kind="bps" /></td>
                    {/* The cross-run board always carries these proxy metrics; the `— ` guards are
                        defensive for the shared LeaderboardRow whose competition variant omits them. */}
                    <td className={styles.num}>{r.sim_pnl === null ? '—' : <Num value={r.sim_pnl} />}</td>
                    <td className={styles.num}>{r.brier === null ? '—' : r.brier.toFixed(2)}</td>
                    <td className={styles.num}>{r.max_drawdown === null ? '—' : <Num value={r.max_drawdown} />}</td>
                    <td className={styles.num}>{r.action_count === null ? '—' : r.action_count}</td>
                    <td className={styles.num}>{r.valid_pct === null ? '—' : `${r.valid_pct.toFixed(1)}%`}</td>
                    <td><ConfBar validCount={r.valid_count} /></td>
                    <td><Badge variant={r.proof_mode} /></td>
                    <td><Badge variant={isEligible(r.proof_mode) ? 'eligible' : 'not-eligible'} /></td>
                    <td><Badge variant={r.anchor_status === 'anchored' ? 'anchored' : r.anchor_status === 'not-anchored' ? 'not-anchored' : 'pending'} /></td>
                    <td data-testid="lb-source">
                      {r.source_mode === 'live' ? <Badge variant="live" />
                        : r.source_mode === 'replay' ? <Badge variant="replay" />
                          : <span className={`${styles.mixedSrc} mono`}>mixed</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <MakerLeaderboard result={makerResult} />
      )}
    </section>
  );
}

// Maker Arena lane (MM-R1) — a SEPARATE render path + data source (SEC-005). Ranks on
// avg_toxicity_loss_bps ASC (lower is better), never CLV; real_executable_edge_bps is always
// the literal `null`; markout is a muted diagnostic column, never the rank axis.
function MakerLeaderboard({ result }: { result: MakerArenaResultView }) {
  const ranked = useMemo(
    () => [...result.leaderboard].sort((a, b) => a.avg_toxicity_loss_bps - b.avg_toxicity_loss_bps),
    [result],
  );
  const f = result.falsification;
  // Badge, headline, and CI copy all derive from the REAL three-state verdict (I-R M1) —
  // an INCONCLUSIVE or INVERTED seal must never render as a separated candidate win.
  const candidate = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'candidate') ?? ranked[0];
  const control = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'control') ?? ranked[1];
  const verdict = deriveMakerVerdict(f, { candidate: candidate.agent_id, control: control.agent_id });

  return (
    <>
      <p className={styles.bannerMaker}>
        Rank is <b>adverse-selection toxicity (lower = better)</b> — a separate lane, <b>not CLV</b>. Mean markout is a diagnostic, not the rank axis.
        Two different lanes, different agents. No fill / PnL / executable-edge claim — <span className="mono">real_executable_edge_bps</span> is null by construction.
      </p>

      <div className={styles.falsificationStrip} data-verdict={verdict.kind.toLowerCase()}>
        <Badge variant={verdict.badge}>{verdict.badgeText}</Badge>
        <div className={styles.falsificationBody}>
          <div className={styles.falsificationHeadline}>{verdict.headline}</div>
          <div className={styles.falsificationSub}>pairwise bootstrap falsification · {verdict.ciSub}</div>
        </div>
        <div className={styles.falsificationStats}>
          <div className={styles.stat}>
            <span className={styles.statLabel}>Δ QUOTE QUALITY</span>
            <Num value={f.delta_bps} kind="bps" />
          </div>
          <div className={styles.stat}>
            <span className={styles.statLabel}>95% CI</span>
            <span className="mono">[{f.ci_low_bps}, {f.ci_high_bps}]</span>
          </div>
          <Badge variant="small-n">n={result.fixture_universe_n} · small sample</Badge>
        </div>
      </div>

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>#</th><th>MAKER AGENT</th>
              <th className={styles.r}>TOXICITY LOSS ↓ <InfoTip label={GLOSSARY.toxicity_loss.label}>{GLOSSARY.toxicity_loss.definition}</InfoTip></th>
              <th className={styles.r}>MARKOUT <InfoTip label={GLOSSARY.mean_markout_diagnostic.label}>{GLOSSARY.mean_markout_diagnostic.definition}</InfoTip></th>
              <th className={styles.r}>QUOTES</th>
              <th className={styles.r}>ABSTAINED</th>
              <th className={styles.r}>EXEC EDGE</th>
              <th>RUNG</th><th></th>
            </tr>
          </thead>
          <tbody>
            {ranked.map((r: MakerLeaderboardRow) => (
              <tr key={r.agent_id} data-testid="lb-maker-row" className={styles.row}>
                <td className="mono" data-testid="lb-maker-rank">{r.maker_rank}</td>
                <td data-testid="lb-maker-agent">
                  <Link href={`/proof/maker/${r.agent_id}`} className={styles.agentLink}>
                    {r.agent_id} ›
                  </Link>
                </td>
                <td className={styles.num} data-testid="lb-maker-toxicity"><Num value={r.avg_toxicity_loss_bps} kind="bps" /></td>
                <td className={`${styles.num} ${styles.diagnostic}`} data-testid="lb-maker-markout">{r.avg_markout_bps}</td>
                <td className={styles.num}>{r.quote_count.toLocaleString()}</td>
                <td className={styles.num}>{r.abstained}</td>
                <td className={`${styles.num} mono`} data-testid="lb-maker-edge">{String(r.real_executable_edge_bps)}</td>
                <td><Badge variant="mm-r1" /></td>
                {/* A real, keyboard-reachable link — never dead arrow text (I-R Min5). */}
                <td className={styles.proofLink}>
                  <Link href={`/proof/maker/${r.agent_id}`} className={styles.proofLinkAnchor} aria-label={`Proof card for ${r.agent_id}`}>
                    PROOF →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className={styles.tableFoot}>
          <span className={styles.footNote}>
            <b>ⓘ MARKOUT is diagnostic only</b> — raw two-sided mean ≈ half_spread/ref (geometry, not quality). naive-mm's markout is higher than txline-fair-mm yet it is more toxic → ranked 2nd. Higher markout ≠ better.
          </span>
          <span className={styles.footCount}>{ranked.length} maker agents · n={result.fixture_universe_n}</span>
        </div>
      </div>
    </>
  );
}
