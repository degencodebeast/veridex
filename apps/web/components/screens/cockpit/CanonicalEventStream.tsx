import Link from 'next/link';
import { inspectorHref } from '@/lib/deeplinks';
import { shortHash } from '@/lib/format';
import type { CanonicalEvent } from '@/lib/contracts';
import styles from './CanonicalEventStream.module.css';

function Cells({ event }: { event: CanonicalEvent }) {
  return (
    <>
      <span className={`${styles.seq} mono`}>{event.seq}</span>
      <span className={`${styles.type} mono`}>{event.type}</span>
      <span className={`${styles.hash} mono`}>{shortHash(event.payload_hash)}</span>
      <span className={`${styles.ev} ${event.evidence ? styles.evYes : styles.evNo} mono`}>
        {event.evidence ? 'evidence' : 'derived'}
      </span>
      {event.summary ? <span className={styles.summary}>{event.summary}</span> : <span />}
    </>
  );
}

export function CanonicalEventStream({ runId, events }: { runId: string; events: CanonicalEvent[] }) {
  return (
    <section className={styles.panel} aria-label="Canonical event stream">
      <div className={styles.head}><span className={styles.sectionLabel}>CANONICAL EVENT STREAM</span></div>
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
