import { fmtBps, signClass } from '@/lib/format';
import type { PerformanceMetrics } from '@/lib/contracts';
import styles from './PerformanceMetricsBlock.module.css';

const NUM = { pos: styles.pos, neg: styles.neg, zero: styles.zero };

export function PerformanceMetricsBlock({ metrics }: { metrics: PerformanceMetrics }) {
  return (
    <section className={styles.block} aria-label="Performance Metrics">
      <div className={styles.head}>
        <span className={styles.title}>PERFORMANCE METRICS</span>
        <span className={styles.subtitle}>how the agent performed — not a trust guarantee</span>
      </div>
      <dl className={styles.grid}>
        <div className={styles.metric}>
          <dt className={styles.label}>CLV <span className={styles.ranked}>ranked</span></dt>
          <dd className={`${styles.value} mono ${NUM[signClass(metrics.clv_bps)]}`}>{fmtBps(metrics.clv_bps)}</dd>
        </div>
        <div className={styles.metric}>
          <dt className={styles.label}>Sim PnL ⓟ</dt>
          <dd className={`${styles.value} mono ${NUM[signClass(metrics.sim_pnl)]}`}>{metrics.sim_pnl.toFixed(1)}</dd>
        </div>
        <div className={styles.metric}>
          <dt className={styles.label}>Brier ⓟ</dt>
          <dd className={`${styles.value} mono`}>{metrics.brier.toFixed(3)}</dd>
        </div>
        <div className={styles.metric}>
          <dt className={styles.label}>Hit Rate</dt>
          <dd className={`${styles.value} mono`}>{(metrics.hit_rate * 100).toFixed(0)}%</dd>
        </div>
        <div className={styles.metric}>
          <dt className={styles.label}>Max Drawdown</dt>
          <dd className={`${styles.value} mono ${NUM[signClass(metrics.max_drawdown)]}`}>{metrics.max_drawdown.toFixed(1)}</dd>
        </div>
      </dl>
      <p className={styles.footer}>ⓟ Sim PnL &amp; Brier are simulated proxies. Checks certify the run is valid; metrics show performance. Rank is Avg CLV only.</p>
    </section>
  );
}
