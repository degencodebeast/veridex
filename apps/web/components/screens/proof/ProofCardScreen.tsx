'use client';
import { useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { InfoTip } from '@/components/ui/InfoTip';
import { shortHash } from '@/lib/format';
import { deriveProofChain } from '@/lib/proof';
import { GLOSSARY } from '@/lib/glossary';
import { PROOF_ROOT_ORDER, PROOF_ROOT_LABELS, EMPTY_ROOT } from '@/lib/fixtures/proof';
import type { AnchorInfo, ProofArtifact, VerifyResult } from '@/lib/contracts';
import { ProofChainStepper } from './ProofChainStepper';
import { ProofChecksBlock } from './ProofChecksBlock';
import { PerformanceMetricsBlock } from './PerformanceMetricsBlock';
import { OnChainValidationBlock } from './OnChainValidationBlock';
import { AnchorPanel } from './AnchorPanel';
import { VerifyButton } from './VerifyButton';
import { ProofExplainer } from './ProofExplainer';
import styles from './ProofCardScreen.module.css';

export function ProofCardScreen({ artifact }: { artifact: ProofArtifact }) {
  // The manifest hash is not in the wire proof artifact; it is revealed by the
  // authoritative verify. Thread it (and the derived chain) once verify returns;
  // honest "verify to reveal" before (gap decision).
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const manifestHash = verify?.manifest_hash || artifact.manifest_hash || '';
  const chain = deriveProofChain(artifact, manifestHash);
  const anchor: AnchorInfo = { ...artifact.anchor, manifest_hash: manifestHash || null };

  // The 6 named Merkle roots, in the canonical PROOF_ROOT_ORDER (backend root-forest domains).
  // Absent ([]) in live until the API serializes the forest → honest-empty state below.
  const rootByDomain = new Map(artifact.roots.map((r) => [r.domain, r]));
  const orderedRoots = PROOF_ROOT_ORDER.map((d) => rootByDomain.get(d)).filter((r) => r !== undefined);

  return (
    <article className={styles.proof}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Proof Card</h1>
          <span className={`${styles.meta} mono`}>{artifact.run_id} · verifier {artifact.verifier_version}</span>
          <Badge variant={artifact.proof_mode} />
          <InfoTip label={GLOSSARY.proof_mode.label}>{GLOSSARY.proof_mode.definition}</InfoTip>
        </div>
        <VerifyButton runId={artifact.run_id} onVerified={setVerify} />
      </header>

      <ProofChainStepper chain={chain} />

      <div className={styles.grid}>
        <div className={styles.left}>
          <section className={styles.metaPanel} aria-label="Artifact metadata">
            <div className={styles.metaGrid}>
              <span className={styles.metaLabel}>verifier <InfoTip label={GLOSSARY.verifier_version.label}>{GLOSSARY.verifier_version.definition}</InfoTip></span><span className={`${styles.metaVal} mono`}>{artifact.verifier_version}</span>
              <span className={styles.metaLabel}>run events</span><span className={`${styles.metaVal} mono`}>{artifact.run_event_count}</span>
              <span className={styles.metaLabel}>evidence hash</span><span className={`${styles.metaVal} mono`}>{shortHash(artifact.evidence_hash)}</span>
              <span className={styles.metaLabel}>manifest hash <InfoTip label={GLOSSARY.anchor.label}>{GLOSSARY.anchor.definition}</InfoTip></span><span className={`${styles.metaVal} mono`}>{manifestHash ? shortHash(manifestHash) : 'verify to reveal'}</span>
              <span className={styles.metaLabel}>source mode</span><span className={`${styles.metaVal} mono`}>{artifact.source_mode}</span>
            </div>
          </section>
          <div className={styles.checksMetricsNote}>
            <span className={styles.noteLabel}>CHECKS ≠ METRICS</span>
            <InfoTip label={GLOSSARY.checks_vs_metrics.label}>{GLOSSARY.checks_vs_metrics.definition}</InfoTip>
          </div>
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
          <section className={styles.roots} aria-label="Merkle roots">
            <span className={styles.sectionLabel}>MERKLE ROOTS <InfoTip label={GLOSSARY.anchor.label}>{GLOSSARY.anchor.definition}</InfoTip></span>
            {orderedRoots.length === 0 ? (
              <p className={styles.rootsEmpty}>root forest computed internally; not yet surfaced by the API</p>
            ) : (
              <div className={styles.rootsGrid}>
                {orderedRoots.map((r) => {
                  const empty = r.root === EMPTY_ROOT;
                  return (
                    <div key={r.domain} className={styles.rootRow} data-empty={empty}>
                      <span className={styles.rootLabel}>{PROOF_ROOT_LABELS[r.domain] ?? r.label}</span>
                      <span className={`${styles.rootHash} mono`}>{shortHash(r.root)}</span>
                      {empty ? <span className={styles.rootEmptyTag}>empty · no records</span> : null}
                    </div>
                  );
                })}
              </div>
            )}
          </section>
          {/* Read-only educational narrator — fenced (never verifies). Cites the deterministic
              Verify result; validity questions are short-circuited to a fixed non-LLM template. */}
          <ProofExplainer runId={artifact.run_id} verified={verify?.verified ?? null} />
        </div>
      </div>
    </article>
  );
}
