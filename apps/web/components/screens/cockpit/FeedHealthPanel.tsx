import type { FeedHealthState, WsStatus } from '@/lib/contracts';
import styles from './FeedHealthPanel.module.css';

// T10 AC-2D-104: binds a live FeedHealthReport honestly. wsStatus is the real-time signal (the
// WS connection itself); feedHealth.stale is the REST-observed staleness snapshot merged with it
// by useArenaStream. Either one being bad renders the stale/reconnect state — NEVER a frozen
// "live-looking" view when the feed genuinely isn't fresh (the arena broadcast is fire-and-forget
// with bounded per-connection queues, so a slow client can be silently dropped — ws.py design).
export function FeedHealthPanel({ feedHealth, wsStatus }: { feedHealth: FeedHealthState; wsStatus: WsStatus }) {
  const reconnecting = wsStatus === 'reconnecting';
  const disconnected = wsStatus === 'disconnected';
  const showStale = feedHealth.stale || reconnecting || disconnected;

  return (
    <section className={styles.panel} aria-label="Feed health">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>FEED HEALTH</span>
        <span className={`${styles.mode} mono`}>{feedHealth.source_mode === 'live' ? 'LIVE' : 'REPLAY'}</span>
      </div>
      {showStale ? (
        <p className={styles.stale} data-testid="feed-stale" role="status">
          {reconnecting
            ? 'reconnecting — resubscribing to the live stream'
            : disconnected
              ? 'disconnected — feed may be stale'
              : 'feed stale'}
          {feedHealth.staleness_s != null ? ` · ${feedHealth.staleness_s}s since last tick` : ''}
        </p>
      ) : (
        <p className={styles.ok} data-testid="feed-ok">feed connected</p>
      )}
      <div className={styles.meta}>
        <span className="mono">{feedHealth.connected ? 'connected' : 'disconnected'}</span>
        <span className="mono">fixture {feedHealth.fixture_id ?? '—'}</span>
        <span className="mono">{feedHealth.ticks_seen} ticks</span>
      </div>
    </section>
  );
}
