import { Badge } from '@/components/ui/Badge';
import { LiveDot } from '@/components/ui/LiveDot';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
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
        <InfoTip label={GLOSSARY.source_mode.label}>{GLOSSARY.source_mode.definition}</InfoTip>
        <span className={`${styles.exec} mono`}>{EXEC_LABEL[header.execution_mode] ?? header.execution_mode}</span>
        <InfoTip label={GLOSSARY.execution_mode.label}>{GLOSSARY.execution_mode.definition}</InfoTip>
        <Badge variant={header.proof_mode} />
        <InfoTip label={GLOSSARY.proof_mode.label}>{GLOSSARY.proof_mode.definition}</InfoTip>
        <span className={`${styles.ws} ${ok ? styles.wsOk : styles.wsWarn} mono`}>
          {ok ? <LiveDot size={5} label="WebSocket connected" /> : null}
          WS {wsStatus}
        </span>
      </div>
      <div className={styles.stats}>
        <span className={`${styles.stat} mono`}>{header.events} events</span>
        {/* valid_pct is a PERCENT (0-100), matching the wire convention. */}
        <span className={`${styles.stat} mono`}>{Math.round(header.valid_pct)}% valid</span>
      </div>
    </header>
  );
}
