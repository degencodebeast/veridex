import { ProofCheckChip } from '@/components/ui/ProofCheckChip';
import { shortHash } from '@/lib/format';
import type { ProofChainStep } from '@/lib/contracts';
import styles from './ProofChainStepper.module.css';

export function ProofChainStepper({ chain }: { chain: ProofChainStep[] }) {
  return (
    <section className={styles.chain} aria-label="Proof chain">
      {chain.map((step, i) => (
        <div key={step.id} className={styles.step}>
          <div className={styles.chip}>
            {step.id === 'anchor' ? <span className={styles.anchorGlyph} aria-hidden>◆</span> : null}
            <ProofCheckChip status={step.status} />
          </div>
          <div className={styles.body}>
            <span className={styles.label}>{step.label}</span>
            <span className={styles.sub}>{step.sub}</span>
            <span className={`${styles.hash} mono`}>{shortHash(step.hash)}</span>
          </div>
          {i < chain.length - 1 ? <span className={styles.arrow} aria-hidden>→</span> : null}
        </div>
      ))}
    </section>
  );
}
