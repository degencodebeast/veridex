'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { ConfBar } from '@/components/ui/ConfBar';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { rankByAvgClv, isEligible } from '@/lib/derive';
import { LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import type { LeaderboardRow } from '@/lib/catalog';
import styles from './LeaderboardScreen.module.css';

type Filter = 'ALL' | 'REPLAY' | 'LIVE';

export function LeaderboardScreen({ rows = LEADERBOARD_ROWS }: { rows?: LeaderboardRow[] }) {
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
        <SegmentedControl<Filter>
          ariaLabel="Source filter"
          value={filter}
          onChange={setFilter}
          options={[{ value: 'ALL', label: 'ALL' }, { value: 'REPLAY', label: 'REPLAY' }, { value: 'LIVE', label: 'LIVE' }]}
        />
      </header>

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
                <td className={styles.num} data-testid="lb-clv"><Num value={r.avg_clv_bps} kind="bps" /></td>
                <td className={styles.num}><Num value={r.total_clv_bps} kind="bps" /></td>
                <td className={styles.num}><Num value={r.sim_pnl} /></td>
                <td className={styles.num}>{r.brier.toFixed(2)}</td>
                <td className={styles.num}><Num value={r.max_drawdown} /></td>
                <td className={styles.num}>{r.action_count}</td>
                <td className={styles.num}>{r.valid_pct.toFixed(1)}%</td>
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
    </section>
  );
}
