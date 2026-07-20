'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ApiError, getInstance, type DeployedInstance } from '@/lib/api';
import { ExecutionEvidenceSection } from './ExecutionEvidenceSection';
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
  // A maker (quoteguard-mm) instance is signalled by a resolved maker tape_ref — the same signal that
  // gates the Maker-tape section below. Makers have NO directional proof card at /proof/{run_id} (it
  // 404s); their behavior evidence is the QuoteGuard Ablation, surfaced as an explicit header action.
  const isMaker = Boolean(inst.maker_tape_ref);
  return (
    <section className={styles.screen} aria-label={`Deployed instance ${inst.instance_id}`}>
      <header className={styles.head}>
        <div className={styles.ident}>
          <span className={styles.eyebrow}>Deployed instance · owner-scoped</span>
          <h1 className={`${styles.title} mono`}>{inst.instance_id}</h1>
          {inst.fixture_label && (
            // CURATED human label — augments the raw ids, never replaces them (the raw fixture id
            // stays visible in the Scope section below, in mono).
            <span className={styles.sub} data-testid="instance-fixture-label">
              {inst.fixture_label}{inst.market_label ? ` · ${inst.market_label}` : ''}
            </span>
          )}
          <span className={styles.sub}>{inst.template_id} · {inst.agent_id} · {inst.execution_mode}</span>
        </div>
        <div className={styles.headMeta}>
          {isMaker && (
            <Link href={`/proof/maker-ablation/${inst.instance_id}`} className={styles.ablationCta} data-testid="instance-ablation-link">
              QuoteGuard Ablation →
            </Link>
          )}
          <span className={styles.status} data-status={inst.status} data-testid="instance-status">{inst.status}</span>
          <span className={styles.source} data-source={inst.source_mode}>{inst.source_mode}</span>
        </div>
      </header>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Evidence identity</h2>
        <div className={styles.kvRow}>
          <span className={styles.kvLabel}>Run</span>
          {isMaker ? (
            // Maker runs have no directional proof card — surface run_id as the plain evidence identity
            // and route the judge to behavior evidence via the QuoteGuard Ablation action (header), never
            // a link to /proof/{run_id} that would 404.
            <span className={`${styles.kvVal} mono`} data-testid="instance-run-id">{inst.run_id}</span>
          ) : (
            <Link href={`/proof/${inst.run_id}`} className={`${styles.kvLink} mono`} data-testid="instance-run-link">{inst.run_id} ›</Link>
          )}
        </div>
        <p className={styles.note}>
          <code className="mono">run_id</code> is the authoritative Veridex evidence identity — it names the sealed run and never changes.
        </p>
        {isMaker && (
          <p className={styles.note} data-testid="instance-maker-run-note">
            This is a maker run — its behavior evidence is the <strong>QuoteGuard Ablation</strong> (the action above), not a directional proof card.
          </p>
        )}
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
        {inst.replay_pack_content_hash && (
          <div className={styles.kvRow} data-testid="instance-pack-hash"><span className={styles.kvLabel}>replay pack content_hash</span><span className={`${styles.kvVal} mono`}>{inst.replay_pack_content_hash}</span></div>
        )}
        {inst.replay_pack_id && (
          <div className={styles.kvRow} data-testid="instance-pack-id"><span className={styles.kvLabel}>replay pack_id</span><span className={`${styles.kvVal} mono`}>{inst.replay_pack_id}</span></div>
        )}
        <div className={styles.kvRow}><span className={styles.kvLabel}>Owner</span><span className={`${styles.kvVal} mono`}>{inst.operator_id ?? '—'}</span></div>
        <div className={styles.kvRow}><span className={styles.kvLabel}>Deployed</span><span className={`${styles.kvVal} mono`}>{inst.created_at}</span></div>
      </section>

      {/* Maker tape — a DISTINCT identity from the replay pack. Kept OUT of "Pinned configuration"
          because it is re-resolved + content-verified at READ time (not a pinned/sealed record field). */}
      {inst.maker_tape_ref && (
        <section className={styles.panel}>
          <h2 className={styles.h2}>Maker tape</h2>
          <div className={styles.kvRow} data-testid="instance-maker-tape-ref"><span className={styles.kvLabel}>maker tape_ref</span><span className={`${styles.kvVal} mono`}>{inst.maker_tape_ref}</span></div>
          {inst.maker_tape_content_hash && (
            <div className={styles.kvRow} data-testid="instance-maker-tape-hash"><span className={styles.kvLabel}>maker tape content_hash</span><span className={`${styles.kvVal} mono`}>{inst.maker_tape_content_hash}</span></div>
          )}
          <p className={styles.note}>
            The maker tape hash is re-resolved from <code className="mono">tape_ref</code> and content-verified at read time — it is the tape the run resolves, not a pinned record field. When it cannot be verified against the tape events, the hash is omitted (never shown mismatched).
          </p>
        </section>
      )}

      <ExecutionEvidenceSection instanceId={inst.instance_id} />

      <section className={styles.panel}>
        <h2 className={styles.h2}>Scope</h2>
        {inst.fixture_label && (
          <div className={styles.kvRow} data-testid="instance-fixture-row">
            <span className={styles.kvLabel}>Fixture</span>
            <span className={styles.kvVal}>
              {inst.fixture_label}{inst.market_label ? ` · ${inst.market_label}` : ''}
              {inst.fixture_id != null && <span className={`${styles.kvVal} mono`}> ({inst.fixture_id})</span>}
            </span>
          </div>
        )}
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
