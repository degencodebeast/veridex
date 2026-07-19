import { Badge } from '@/components/ui/Badge';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
import { fmtBps, signClass } from '@/lib/format';
import type { LeaderboardRow } from '@/lib/contracts';
import styles from './ClvLeaderboard.module.css';

const NUM = { pos: styles.pos, neg: styles.neg, zero: styles.zero };

// Honest sentinel for a metric the competition-scoped wire row does NOT carry — never a fabricated 0.
const DASH = '—';
const numClass = (n: number | null) => NUM[n === null ? 'zero' : signClass(n)];

export function ClvLeaderboard({ rows }: { rows: LeaderboardRow[] }) {
  // SEC-005: rank == Avg CLV only; eligibility AND confidence never reorder. F-5 / CON-203: the
  // competition leaderboard is BACKEND-AUTHORITATIVE — the server ranks + orders the rows and we
  // render that order (and its `rank`) VERBATIM. NEVER re-sort locally: a client CLV re-sort would
  // silently disagree with the sealed leaderboard the arena is proving (hard honesty rule).
  return (
    <section className={styles.wrap} aria-label="CLV leaderboard">
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.thNum}>#</th>
            <th className={styles.thAgent}>AGENT</th>
            <th className={styles.thNum}>AVG CLV <InfoTip label={GLOSSARY.clv.label}>{GLOSSARY.clv.definition}</InfoTip></th>
            <th className={styles.thNum}>TOTAL CLV</th>
            <th className={styles.thNum}>SIM PNL ⓟ</th>
            <th className={styles.thNum}>BRIER ⓟ</th>
            <th className={styles.thNum}>MAX DD</th>
            <th className={styles.thNum}>ACT</th>
            <th className={styles.thNum}>VALID</th>
            <th>PROOF <InfoTip label={GLOSSARY.checks_vs_metrics.label}>{GLOSSARY.checks_vs_metrics.definition}</InfoTip></th>
            <th>ANCHOR <InfoTip label={GLOSSARY.anchor.label}>{GLOSSARY.anchor.definition}</InfoTip></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.agent_id}>
              <td className={`${styles.num} mono`}>{r.rank}</td>
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
              {/* sim_pnl / brier / max_drawdown / action_count / valid_pct are NOT in the
                  competition-scoped wire row — a null renders an honest em-dash, never a fake 0. */}
              <td className={`${styles.num} mono ${numClass(r.sim_pnl)}`}>{r.sim_pnl === null ? DASH : r.sim_pnl.toFixed(1)}</td>
              <td className={`${styles.num} mono`}>{r.brier === null ? DASH : r.brier.toFixed(3)}</td>
              <td className={`${styles.num} mono ${numClass(r.max_drawdown)}`}>{r.max_drawdown === null ? DASH : r.max_drawdown.toFixed(1)}</td>
              <td className={`${styles.num} mono`}>{r.action_count === null ? DASH : r.action_count}</td>
              {/* valid_pct is a PERCENT (0-100) on the cross-run row; the competition row has no percent. */}
              <td className={`${styles.num} mono`}>{r.valid_pct === null ? DASH : `${r.valid_pct}%`}</td>
              <td><Badge variant={r.proof_mode} /></td>
              <td>
                {r.anchor_status === 'anchored' ? <Badge variant="anchored" />
                  : r.anchor_status === 'pending' ? <Badge variant="pending" />
                  : r.anchor_status === 'not_applicable' ? <span className={styles.naBadge}>n/a</span>
                  : <Badge variant="not-anchored" />}
              </td>
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
