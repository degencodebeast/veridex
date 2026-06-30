import { Badge } from '@/components/ui/Badge';
import { LiveDot } from '@/components/ui/LiveDot';
import type { RunHeaderState, WsStatus } from '@/lib/contracts';
import styles from './RunHeader.module.css';

const EXEC_LABEL: Record<string, string> = { paper: 'PAPER', dry_run: 'DRY RUN', live_guarded: 'LIVE GUARDED' };

export function RunHeader({ header, wsStatus }: { header: RunHeaderState; wsStatus: WsStatus }) {
  const ok = wsStatus === 'connected';
  return (
    <header className={styles.header}>
      <div className={styles.titleRow}>
        <h1 className={styles.title}>{header.fixture}</h1>
        <span className={`${styles.competition} mono`}>{header.competition}</span>
      </div>
      <div className={styles.badges}>
        <Badge variant={header.source_mode === 'live' ? 'live' : 'replay'} />
        <span className={`${styles.exec} mono`}>{EXEC_LABEL[header.execution_mode] ?? header.execution_mode}</span>
        <Badge variant={header.proof_mode} />
        <span className={`${styles.ws} ${ok ? styles.wsOk : styles.wsWarn} mono`}>
          {ok ? <LiveDot size={5} label="WebSocket connected" /> : null}
          WS {wsStatus}
        </span>
      </div>
      <div className={styles.stats}>
        <span className={`${styles.stat} mono`}>{header.events} events</span>
        <span className={`${styles.stat} mono`}>{Math.round(header.valid_pct * 100)}% valid</span>
      </div>
    </header>
  );
}
