'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { ConfBar } from '@/components/ui/ConfBar';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { rankByAvgClv } from '@/lib/derive';
import { MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { deriveMakerVerdict } from '@/lib/makerVerdict';
import { GLOSSARY } from '@/lib/glossary';
import { useLane, type Lane } from '@/hooks/useLane';
import { useMakerArenaResult } from '@/hooks/useMakerArenaResult';
import type { LeaderboardRow, ProofState } from '@/lib/catalog';
import type { MakerArenaResultView, MakerLeaderboardRow } from '@/lib/contracts';
import styles from './LeaderboardScreen.module.css';

type Filter = 'ALL' | 'REPLAY' | 'LIVE';

export function LeaderboardScreen({
  // Directional rows are supplied by the page via the self-gating getLeaderboard() reader (mock ON →
  // fixture; mock OFF → real fetch, honest-empty on absence/error). No fixture DEFAULT here — an
  // absent `rows` renders an honest-empty board, never fabricated rankings (T-2 fixture prohibition).
  rows = [],
  // Maker result is page-sourced via useMakerArenaResult (F-9). No sealed-fixture default here —
  // an absent `makerResult` triggers the honest live-fetch/honest-empty maker path, never a fixture.
  makerResult,
}: {
  // The off-mock DIRECTIONAL board supplies DirectionalRow[] (LeaderboardRow + an honest roster-local
  // proof_state); the mock/global path supplies plain LeaderboardRow[] (proof_state absent). Accept both
  // via an optional proof_state so the PROOF badge can render the HONEST 'mixed'/'unknown' when present
  // (Gate-3 M3) and fall back to the shared proof_mode on the mock path.
  rows?: Array<LeaderboardRow & { proof_state?: ProofState }>;
  makerResult?: MakerArenaResultView;
}) {
  const [lane, setLane] = useLane();
  const [filter, setFilter] = useState<Filter>('ALL');
  const makerState = useMakerArenaResult(lane === 'maker', makerResult);

  const ranked = useMemo(() => {
    const scoped = filter === 'ALL'
      ? rows
      : rows.filter((r) => r.source_mode === filter.toLowerCase());
    // rankByAvgClv sorts by avg_clv_bps (SEC-005) and only spreads + stamps `rank`, so the honest
    // roster-local proof_state rides through untouched at runtime; its LeaderboardRow[] return type
    // just omits the optional field, so re-assert the input row type (same objects, sound).
    return rankByAvgClv(scoped) as Array<LeaderboardRow & { proof_state?: ProofState }>;
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

          {ranked.length === 0 ? (
            // Honest-empty: no fabricated rows when the reader has nothing to show (mock OFF with no
            // backend rows, or a fetch error). NEVER the LEADERBOARD_ROWS fixture (T-2).
            <p className={styles.empty} data-testid="lb-empty">
              No ranked agents yet.
            </p>
          ) : (
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
                    {/* HONEST proof surface (Gate-3 M3): the off-mock DirectionalRow carries a
                        roster-local proof_state that preserves the backend's 'mixed'/'unknown'
                        aggregate; render it when present, else the mock/global path's proof_mode.
                        NEVER coerce 'mixed' up to an unearned 'reproducible' on the board. */}
                    <td><Badge variant={r.proof_state ?? r.proof_mode} /></td>
                    {/* II-W defect 5: render the BACKEND-authoritative eligibility_badge VERBATIM
                        (anchor-derived server-side — veridex/leaderboard.py). NEVER re-derive it from
                        proof_mode here; that reversed the adapter fix and disagreed with the backend. */}
                    <td><Badge variant={r.eligibility_badge} /></td>
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
          <MakerLeaderboard result={makerState.result} />
        )
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
