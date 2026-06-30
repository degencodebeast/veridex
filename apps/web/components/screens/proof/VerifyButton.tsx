'use client';
import { useState } from 'react';
import { verifyProof } from '@/lib/api';
import { ProofCheckChip } from '@/components/ui/ProofCheckChip';
import { CHECK_LABELS } from '@/lib/checks';
import { fmtBps } from '@/lib/format';
import type { VerifyResult } from '@/lib/contracts';
import styles from './VerifyButton.module.css';

type State =
  | { kind: 'idle' } | { kind: 'loading' }
  | { kind: 'done'; result: VerifyResult } | { kind: 'error' };

export function VerifyButton({ runId, onVerified }: { runId: string; onVerified?: (r: VerifyResult) => void }) {
  const [state, setState] = useState<State>({ kind: 'idle' });

  async function run() {
    setState({ kind: 'loading' });
    try {
      // Authoritative backend recompute — the frontend NEVER reimplements the law (CON-003).
      const result = await verifyProof(runId);
      setState({ kind: 'done', result });
      onVerified?.(result);
    } catch {
      setState({ kind: 'error' });
    }
  }

  const result = state.kind === 'done' ? state.result : null;
  // D1 carry: wire `verified` is evidence-hash-only. A tampered score leaves
  // verified=true but a blocking check (metrics_recomputed) fails — so the headline
  // is verified AND no blocking check failed, never the boolean alone.
  const blockingFailed = result ? result.checks.some((c) => c.severity === 'blocking' && c.result === 'fail') : false;
  const fullyVerified = result ? result.verified && !blockingFailed : false;

  return (
    <div className={styles.wrap}>
      <button type="button" className={styles.btn} onClick={run} disabled={state.kind === 'loading'}>
        {state.kind === 'loading' ? 'Verifying…' : 'Verify'}
      </button>

      {result ? (
        <div className={styles.result}>
          <p className={fullyVerified ? styles.headlineOk : styles.headlineBad}>
            {fullyVerified ? '✓ Verified' : '⚠ NOT fully verified — a blocking check failed'}
          </p>
          <p className={result.evidence_hash_confirmed ? styles.ok : styles.bad}>
            {result.evidence_hash_confirmed ? '✓ evidence hash confirmed' : '✗ evidence hash mismatch'}
          </p>
          <p className={result.manifest_hash_confirmed ? styles.ok : styles.bad}>
            {result.manifest_hash_confirmed ? '✓ manifest hash confirmed' : '✗ manifest hash mismatch'}
          </p>
          <p className={`${styles.recompute} mono`}>
            recomputed edge {fmtBps(result.recomputed.recomputed_edge_bps)} · CLV {fmtBps(result.recomputed.clv_bps)} · {result.recomputed.valid ? 'valid' : 'invalid'}
          </p>
          {/* Per-check results from the verify response (not just the boolean). */}
          <ul className={styles.checks}>
            {result.checks.map((c) => (
              <li key={c.id} className={styles.checkRow}>
                <ProofCheckChip status={c.result} />
                <span className={styles.checkLabel}>{CHECK_LABELS[c.id] ?? c.label}</span>
              </li>
            ))}
          </ul>
          {result.explorer_url ? (
            <a className={styles.tx} href={result.explorer_url} target="_blank" rel="noreferrer">Open Solana tx →</a>
          ) : null}
        </div>
      ) : null}

      {state.kind === 'error' ? <p className={styles.bad}>Verification failed — try again.</p> : null}
    </div>
  );
}
