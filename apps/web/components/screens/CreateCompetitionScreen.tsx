'use client';
import { useState } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { Badge } from '@/components/ui/Badge';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';
import type { CompetitionType, ExecutionMode, ProofMode } from '@/lib/catalog';
import styles from './CreateCompetitionScreen.module.css';

type SourceMode = 'replay' | 'live';

function proofFor(type: CompetitionType, source: SourceMode): ProofMode {
  if (source === 'replay' || type === 'replay_arena') return 'reproducible';
  return 'verified';
}

export interface CreateCompetitionCommit {
  competition_type: CompetitionType;
  source_mode: SourceMode;
  execution_mode: ExecutionMode;
  // SEC-009: commit exactly what is pinned. proof_mode is deterministic from type+source
  // and is shown in the pinned block, so it travels with the commit. The policy envelope is
  // the default (DEFAULT_POLICY_ENVELOPE), applied backend-side at run creation.
  proof_mode: ProofMode;
}

export function CreateCompetitionScreen({ onCommit = () => {} }: { onCommit?: (cfg: CreateCompetitionCommit) => void }) {
  const [type, setType] = useState<CompetitionType>('live_arena');
  const [source, setSource] = useState<SourceMode>('live');
  const [exec, setExec] = useState<ExecutionMode>('paper');
  const proof = proofFor(type, source);

  return (
    <section className={styles.screen} aria-label="Create Competition">
      <h1 className={styles.title}>Create Competition</h1>

      <div className={styles.picker}>
        <label className={styles.field}>
          <span className={styles.label}>Type</span>
          <SegmentedControl<CompetitionType>
            ariaLabel="Competition type" value={type} onChange={setType}
            options={[
              { value: 'live_arena', label: 'Live' }, { value: 'replay_arena', label: 'Replay' },
              { value: 'head_to_head', label: 'Head-to-Head' }, { value: 'prize_vault_challenge', label: 'Prize-Vault' },
            ]}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Source</span>
          <SegmentedControl<SourceMode>
            ariaLabel="Source mode" value={source} onChange={setSource}
            options={[{ value: 'live', label: 'Live' }, { value: 'replay', label: 'Replay' }]}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Execution</span>
          <SegmentedControl<ExecutionMode>
            ariaLabel="Execution mode" value={exec} onChange={setExec}
            options={[{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }, { value: 'live_guarded', label: 'Live Guarded' }]}
          />
        </label>
      </div>

      <div className={styles.pinned} data-testid="pinned-config">
        <h2 className={styles.h2}>Pinned before entry</h2>
        <div className={styles.pins}>
          <span className={styles.pin}>LAW deterministic recompute</span>
          <span className={styles.pin}>POLICY min_edge {DEFAULT_POLICY_ENVELOPE.min_edge_bps} bps · kill {String(DEFAULT_POLICY_ENVELOPE.kill_switch)}</span>
          <span className={styles.pin}>PROOF <Badge variant={proof} /></span>
          <span className={styles.pin}>EXEC {exec}</span>
        </div>
        <p className={styles.note}>These are frozen at entry. Changing config after a run starts creates a new version (SEC-009).</p>
      </div>

      <button type="button" className={styles.commit} onClick={() => onCommit({ competition_type: type, source_mode: source, execution_mode: exec, proof_mode: proof })}>
        Commit &amp; Enter →
      </button>
    </section>
  );
}
