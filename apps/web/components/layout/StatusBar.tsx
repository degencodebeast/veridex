'use client';
import { Badge } from '@/components/ui/Badge';
import { DirectionToggle } from '@/components/ui/DirectionToggle';
import { VERIFIER_VERSION } from '@/lib/status';
import type { WsStatus } from '@/lib/contracts';
import { useStatusBar } from './StatusBarContext';
import styles from './StatusBar.module.css';

// Honest WS label — "CONNECTED · seq N" ONLY when truly connected; never fabricated otherwise.
function wsLabel(ws: WsStatus, seq: number | null): string {
  switch (ws) {
    case 'connected': return seq != null ? `WS CONNECTED · seq ${seq}` : 'WS CONNECTED';
    case 'connecting': return 'WS connecting';
    case 'reconnecting': return 'WS resyncing';
    case 'disconnected': return 'WS offline';
    default: return 'WS idle';
  }
}

export function StatusBar() {
  const { status } = useStatusBar();
  return (
    <div className={styles.bar} role="status" aria-label="Run status" data-testid="status-bar">
      <span className={styles.field} data-testid="status-fixture">
        <span className={styles.tag}>FIXTURE</span>
        <span className={styles.value}>{status?.fixture || status?.competition || '—'}</span>
      </span>
      <span className={styles.sep} aria-hidden>·</span>
      {/* Source axis (REPLAY/LIVE) — honesty-gated upstream (mock ⇒ replay). Idle ⇒ neutral chip. */}
      <span data-testid="status-source">
        {status
          ? <Badge variant={status.sourceMode === 'live' ? 'live' : 'replay'} />
          : <span className={`${styles.idle} mono`}>IDLE</span>}
      </span>
      {status?.scoring && (
        <>
          <span className={styles.sep} aria-hidden>·</span>
          <span className={`${styles.scoring} mono`}>SCORING</span>
        </>
      )}
      <span className={styles.sep} aria-hidden>·</span>
      <span className={`${styles.mono} mono`} data-testid="status-exec">EXEC · {status?.executionMode ?? '—'}</span>
      <span className={styles.sep} aria-hidden>·</span>
      <span className={`${styles.mono} mono`} data-testid="status-ws">{status ? wsLabel(status.ws, status.seq) : 'WS idle'}</span>
      <span className={styles.spacer} />
      <span className={`${styles.verifier} mono`}>verifier {VERIFIER_VERSION}</span>
      <span className={styles.sep} aria-hidden>·</span>
      <DirectionToggle />
    </div>
  );
}
