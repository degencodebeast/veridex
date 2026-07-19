'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ApiError, getInstance, type DeployedInstance } from '@/lib/api';
import styles from './InstanceScreen.module.css';

// The OWNER-scoped deployed-instance identity. Distinct from the PUBLIC /agents strategy profile:
// this is a real deployed instance the caller owns, fetched with a bearer. run_id is the
// AUTHORITATIVE Veridex evidence identity; runtime_handle.session_id is a REPLACEABLE AgentOS
// handle. On a 403/404 we render an honest unauthorized/not-found state — NEVER a fabricated
// instance and NEVER a fixture fallback.

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; instance: DeployedInstance }
  | { kind: 'error'; status?: number };

// Honest, specific copy per failure — errors name what happened, never apologize or go vague.
function errorCopy(status?: number): { title: string; body: string } {
  if (status === 403) {
    return { title: 'Not your instance', body: "You don't own this deployed instance, so it isn't shown." };
  }
  if (status === 404) {
    return { title: 'Instance not found', body: 'No deployed instance matches this id, or it is not owned by you.' };
  }
  return { title: "Couldn't load instance", body: 'The deployed instance could not be loaded. Check your session and try again.' };
}

export function InstanceScreen({
  instanceId,
  load = getInstance,
}: {
  instanceId: string;
  load?: (id: string) => Promise<DeployedInstance>;
}) {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });

  useEffect(() => {
    let active = true;
    setState({ kind: 'loading' });
    load(instanceId)
      .then((instance) => { if (active) setState({ kind: 'ready', instance }); })
      .catch((err: unknown) => {
        if (active) setState({ kind: 'error', status: err instanceof ApiError ? err.status : undefined });
      });
    return () => { active = false; };
  }, [instanceId, load]);

  if (state.kind === 'loading') {
    return (
      <section className={styles.screen} aria-label="Deployed instance" aria-busy="true">
        <div className={styles.loading} data-testid="instance-loading">Loading deployed instance…</div>
      </section>
    );
  }

  if (state.kind === 'error') {
    const { title, body } = errorCopy(state.status);
    return (
      <section className={styles.screen} aria-label="Deployed instance">
        <div className={styles.error} data-testid="instance-error" role="alert">
          <h1 className={styles.errorTitle}>{title}</h1>
          <p className={styles.errorBody}>{body}</p>
          <Link href="/dashboard" className={styles.back}>Back to Operator Dashboard</Link>
        </div>
      </section>
    );
  }

  const inst = state.instance;
  return (
    <section className={styles.screen} aria-label={`Deployed instance ${inst.instance_id}`}>
      <header className={styles.head}>
        <div className={styles.ident}>
          <span className={styles.eyebrow}>Deployed instance · owner-scoped</span>
          <h1 className={`${styles.title} mono`}>{inst.instance_id}</h1>
          <span className={styles.sub}>{inst.template_id} · {inst.agent_id} · {inst.execution_mode}</span>
        </div>
        <div className={styles.headMeta}>
          <span className={styles.status} data-status={inst.status} data-testid="instance-status">{inst.status}</span>
          <span className={styles.source} data-source={inst.source_mode}>{inst.source_mode}</span>
        </div>
      </header>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Evidence identity</h2>
        <div className={styles.kvRow}>
          <span className={styles.kvLabel}>Run</span>
          <Link href={`/proof/${inst.run_id}`} className={`${styles.kvLink} mono`}>{inst.run_id} ›</Link>
        </div>
        <p className={styles.note}>
          <code className="mono">run_id</code> is the authoritative Veridex evidence identity — it names the sealed run and never changes.
        </p>
        <div className={styles.kvRow}>
          <span className={styles.kvLabel}>Runtime session</span>
          <span className={`${styles.kvVal} mono`}>{inst.runtime_handle?.session_id ?? '—'}</span>
        </div>
        <p className={styles.note}>
          The runtime session is a replaceable AgentOS handle ({inst.runtime_handle?.runtime_kind ?? 'n/a'}) — it may be re-minted on restart under the same run, and is never the result or ownership authority.
        </p>
      </section>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Pinned configuration</h2>
        <div className={styles.kvRow}><span className={styles.kvLabel}>config_hash</span><span className={`${styles.kvVal} mono`}>{inst.config_hash}</span></div>
        <div className={styles.kvRow}><span className={styles.kvLabel}>policy_hash</span><span className={`${styles.kvVal} mono`}>{inst.policy_hash}</span></div>
        <div className={styles.kvRow}><span className={styles.kvLabel}>Owner</span><span className={`${styles.kvVal} mono`}>{inst.operator_id ?? '—'}</span></div>
        <div className={styles.kvRow}><span className={styles.kvLabel}>Deployed</span><span className={`${styles.kvVal} mono`}>{inst.created_at}</span></div>
      </section>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Scope</h2>
        <div className={styles.kvRow}><span className={styles.kvLabel}>Markets</span><span className={styles.tags}>{inst.market_allowlist.length ? inst.market_allowlist.map((m) => <span key={m} className={styles.tag}>{m}</span>) : <span className={styles.kvVal}>—</span>}</span></div>
        <div className={styles.kvRow}><span className={styles.kvLabel}>Venues</span><span className={styles.tags}>{inst.venue_allowlist.length ? inst.venue_allowlist.map((v) => <span key={v} className={styles.tag}>{v}</span>) : <span className={styles.kvVal}>—</span>}</span></div>
      </section>

      {inst.status === 'failed' && inst.last_failure_reason && (
        <section className={`${styles.panel} ${styles.failPanel}`}>
          <h2 className={styles.h2}>Failure</h2>
          <p className={styles.failReason} data-testid="instance-failure">{inst.last_failure_reason}</p>
        </section>
      )}
    </section>
  );
}
