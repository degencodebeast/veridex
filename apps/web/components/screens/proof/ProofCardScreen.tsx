'use client';
import { useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { shortHash } from '@/lib/format';
import { deriveProofChain } from '@/lib/proof';
import type { AnchorInfo, ProofArtifact, VerifyResult } from '@/lib/contracts';
import { ProofChainStepper } from './ProofChainStepper';
import { ProofChecksBlock } from './ProofChecksBlock';
import { PerformanceMetricsBlock } from './PerformanceMetricsBlock';
import { OnChainValidationBlock } from './OnChainValidationBlock';
import { AnchorPanel } from './AnchorPanel';
import { VerifyButton } from './VerifyButton';
import styles from './ProofCardScreen.module.css';

export function ProofCardScreen({ artifact }: { artifact: ProofArtifact }) {
  // The manifest hash is not in the wire proof artifact; it is revealed by the
  // authoritative verify. Thread it (and the derived chain) once verify returns;
  // honest "verify to reveal" before (gap decision).
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const manifestHash = verify?.manifest_hash || artifact.manifest_hash || '';
  const chain = deriveProofChain(artifact, manifestHash);
  const anchor: AnchorInfo = { ...artifact.anchor, manifest_hash: manifestHash || null };

  return (
    <article className={styles.proof}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Proof Card</h1>
          <span className={`${styles.meta} mono`}>{artifact.run_id} · verifier {artifact.verifier_version}</span>
          <Badge variant={artifact.proof_mode} />
        </div>
        <VerifyButton runId={artifact.run_id} onVerified={setVerify} />
      </header>

      <ProofChainStepper chain={chain} />

      <div className={styles.grid}>
        <div className={styles.left}>
          <section className={styles.metaPanel} aria-label="Artifact metadata">
            <div className={styles.metaGrid}>
              <span className={styles.metaLabel}>verifier</span><span className={`${styles.metaVal} mono`}>{artifact.verifier_version}</span>
              <span className={styles.metaLabel}>run events</span><span className={`${styles.metaVal} mono`}>{artifact.run_event_count}</span>
              <span className={styles.metaLabel}>evidence hash</span><span className={`${styles.metaVal} mono`}>{shortHash(artifact.evidence_hash)}</span>
              <span className={styles.metaLabel}>manifest hash</span><span className={`${styles.metaVal} mono`}>{manifestHash ? shortHash(manifestHash) : 'verify to reveal'}</span>
              <span className={styles.metaLabel}>source mode</span><span className={`${styles.metaVal} mono`}>{artifact.source_mode}</span>
            </div>
          </section>
          <ProofChecksBlock checks={artifact.checks} />
          <PerformanceMetricsBlock metrics={artifact.metrics} />
          <OnChainValidationBlock validations={artifact.validations} />
        </div>
        <div className={styles.right}>
          <section className={styles.modeMap} aria-label="Proof mode map">
            <span className={styles.sectionLabel}>PROOF MODE MAP</span>
            <div className={styles.modeRow}><span>reproducible</span><span className="mono">{artifact.proof_mode_map.reproducible}</span></div>
            <div className={styles.modeRow}><span>verified</span><span className="mono">{artifact.proof_mode_map.verified}</span></div>
            <div className={styles.modeRow}><span>partial</span><span className="mono">{artifact.proof_mode_map.partial}</span></div>
          </section>
          <AnchorPanel anchor={anchor} />
          <p className={styles.chatNote}>Proof Chat · READ-ONLY · POST-RUN</p>
        </div>
      </div>
    </article>
  );
}
