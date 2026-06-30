import { Badge } from '@/components/ui/Badge';
import { fmtBps, signClass } from '@/lib/format';
import type { LeaderboardRow } from '@/lib/contracts';
import styles from './ClvLeaderboard.module.css';

const NUM = { pos: styles.pos, neg: styles.neg, zero: styles.zero };

export function ClvLeaderboard({ rows }: { rows: LeaderboardRow[] }) {
  // SEC-005: rank == Avg CLV only; eligibility AND confidence never reorder.
  const ordered = [...rows].sort((a, b) => b.avg_clv_bps - a.avg_clv_bps);
  return (
    <section className={styles.wrap} aria-label="CLV leaderboard">
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.thNum}>#</th>
            <th className={styles.thAgent}>AGENT</th>
            <th className={styles.thNum}>AVG CLV</th>
            <th className={styles.thNum}>TOTAL CLV</th>
            <th className={styles.thNum}>SIM PNL ⓟ</th>
            <th className={styles.thNum}>BRIER ⓟ</th>
            <th className={styles.thNum}>MAX DD</th>
            <th className={styles.thNum}>ACT</th>
            <th className={styles.thNum}>VALID</th>
            <th>PROOF</th>
            <th>ANCHOR</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((r, i) => (
            <tr key={r.agent_id}>
              <td className={`${styles.num} mono`}>{i + 1}</td>
              <td className={styles.agent}>
                <span className={styles.agentName}>{r.agent_name}</span>
                <span className={styles.agentKind}>{r.agent_kind}</span>
                {/* WD-7: low-sample CLV is flagged as low-confidence (display only,
                    never reorders the rank — SEC-005). */}
                {r.low_sample ? (
                  <span className={styles.lowConf} title={`low confidence — only ${r.valid_count} valid actions`}>
                    low-confidence
                  </span>
                ) : null}
              </td>
              <td className={`${styles.num} mono ${NUM[signClass(r.avg_clv_bps)]}`}>{fmtBps(r.avg_clv_bps)}</td>
              <td className={`${styles.num} mono ${NUM[signClass(r.total_clv_bps)]}`}>{fmtBps(r.total_clv_bps)}</td>
              <td className={`${styles.num} mono ${NUM[signClass(r.sim_pnl)]}`}>{r.sim_pnl.toFixed(1)}</td>
              <td className={`${styles.num} mono`}>{r.brier.toFixed(3)}</td>
              <td className={`${styles.num} mono ${NUM[signClass(r.max_drawdown)]}`}>{r.max_drawdown.toFixed(1)}</td>
              <td className={`${styles.num} mono`}>{r.action_count}</td>
              <td className={`${styles.num} mono`}>{r.valid_pct}%</td>
              <td><Badge variant={r.proof_mode} /></td>
              <td><Badge variant={r.anchor_status === 'anchored' ? 'anchored' : r.anchor_status === 'pending' ? 'pending' : 'not-anchored'} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={styles.footer}>
        ⓟ Sim PnL &amp; Brier are simulated proxies — not settled profit. Rank is Avg CLV only; proof completeness gates eligibility, never rank. Low-confidence marks small samples and never changes rank.
      </p>
    </section>
  );
}
