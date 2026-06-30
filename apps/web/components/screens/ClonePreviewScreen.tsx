'use client';
import Link from 'next/link';
import { JsonView } from '@/components/ui/JsonView';
import type { AgentProfileRecord } from '@/lib/catalog';
import styles from './ClonePreviewScreen.module.css';

export function ClonePreviewScreen({
  source, onCommit = () => {},
}: { source: AgentProfileRecord; onCommit?: () => void }) {
  // AC-023: a clone copies CONFIG ONLY — never the source's CLV record/identity. Only config-level
  // fields are surfaced here (no avg_clv/total_clv/runs/valid_count/anchors).
  const copiedConfig = {
    archetype: source.archetype,
    mode: source.mode,
    source: source.source,
    config_hash: source.config_hash,
    policy_hash: source.policy_hash,
  };
  return (
    <section className={styles.screen} aria-label="Clone Preview">
      <header className={styles.head}>
        <Link href={`/agents/${source.agent_id}`} className={styles.back}>← Source Profile</Link>
        <h1 className={styles.title}>Clone {source.agent_name}</h1>
      </header>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Config copied (pinned)</h2>
        <JsonView data={copiedConfig} />
        <p className={styles.copy}>Config copied pinned; <strong>the law recomputes your own CLV</strong>. A clone earns its own record — the source&apos;s Avg CLV is not transferred.</p>
      </section>

      <section className={styles.panel} data-testid="not-copied">
        <h2 className={styles.h2}>Not copied — the clone starts fresh</h2>
        <ul className={styles.list}>
          <li className={styles.item}>The source&apos;s Avg CLV, total CLV, and run record</li>
          <li className={styles.item}>Eligibility, anchors, and historical proofs</li>
          <li className={styles.item}>Identity — the clone is a new agent scored only from its own sealed evidence</li>
        </ul>
      </section>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Terms</h2>
        <div className={styles.kv}><span>Clone-cap</span><span className="mono">3 of 5 used</span></div>
        <div className={styles.kv}><span>Source proof mode</span><span className="mono">{source.proof_mode}</span></div>
        <p className={styles.note}>Cloning copies configuration only. Your run is scored independently from sealed evidence.</p>
      </section>

      <button type="button" className={styles.commit} onClick={onCommit}>Clone into my roster</button>
    </section>
  );
}
