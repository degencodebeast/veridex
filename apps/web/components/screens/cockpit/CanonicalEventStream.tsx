import Link from 'next/link';
import { inspectorHref } from '@/lib/deeplinks';
import { shortHash, fmtBps } from '@/lib/format';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
import type { CanonicalEvent } from '@/lib/contracts';
import styles from './CanonicalEventStream.module.css';

// T10 AC-2D-103: window CLV is NEVER shown as the plain "CLV" label, and a pending row is NEVER
// shown as a fabricated number — both read straight off the single-sourced glossary.
function ClvCell({ clv }: { clv: NonNullable<CanonicalEvent['clv']> }) {
  if (clv.kind === 'pending') {
    return <span className={`${styles.clv} mono`} data-testid="clv-cell">{GLOSSARY.clv_pending.label}</span>;
  }
  const label = clv.kind === 'window_clv' ? GLOSSARY.window_clv.label : GLOSSARY.clv.label;
  return <span className={`${styles.clv} mono`} data-testid="clv-cell">{label} {fmtBps(clv.bps)}</span>;
}

function Cells({ event }: { event: CanonicalEvent }) {
  return (
    <>
      <span className={`${styles.seq} mono`}>{event.seq}</span>
      <span className={`${styles.type} mono`}>{event.type}</span>
      <span className={`${styles.hash} mono`}>{shortHash(event.payload_hash)}</span>
      <span className={`${styles.ev} ${event.evidence ? styles.evYes : styles.evNo} mono`} data-testid="ev-flag">
        {/* ● = sealed-evidence prefix; ○ = derived non-scoring tail (ui-only). Honest, never faked. */}
        {event.evidence ? '● evidence' : '○ ui-only'}
      </span>
      {event.clv ? <ClvCell clv={event.clv} /> : event.summary ? <span className={styles.summary}>{event.summary}</span> : <span />}
    </>
  );
}

export function CanonicalEventStream({ runId, events }: { runId: string; events: CanonicalEvent[] }) {
  return (
    <section className={styles.panel} aria-label="Canonical event stream">
      <div className={styles.head}>
        <span className={styles.sectionLabel}>CANONICAL EVENT STREAM</span>
        <InfoTip label={GLOSSARY.seq.label}>{GLOSSARY.seq.definition}</InfoTip>
      </div>
      <ul className={styles.list}>
        {events.map((event) => {
          const clickable = event.type === 'AGENT_ACTION';
          return (
            <li key={event.seq} className={styles.row}>
              {clickable ? (
                <Link href={inspectorHref(runId, event.seq)} className={`${styles.line} ${styles.clickable}`}>
                  <Cells event={event} />
                  <span className={styles.chevron} aria-hidden>›</span>
                </Link>
              ) : (
                <div className={styles.line}><Cells event={event} /><span /></div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
