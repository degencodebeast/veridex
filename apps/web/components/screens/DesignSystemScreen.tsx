import { Badge } from '@/components/ui/Badge';
import { BADGE_VARIANTS } from '@/lib/badges';
import { ProofCheckChip, type CheckStatus } from '@/components/ui/ProofCheckChip';
import styles from './DesignSystemScreen.module.css';

const SWATCHES = [
  ['bg', '--bg'], ['panel', '--panel'], ['panel-2', '--panel-2'], ['border', '--border'],
  ['border-strong', '--border-strong'], ['text-1', '--text-1'], ['text-2', '--text-2'],
  ['text-3', '--text-3'], ['accent', '--accent'], ['warning', '--warning'],
  ['positive', '--positive'], ['negative', '--negative'],
];

const TYPE_SPECIMENS = [
  ['Screen title · 19/600', styles.title, 'Veridex Proof Arena'],
  ['Panel heading · 11–14/600', styles.heading, 'PROOF CHECKS'],
  ['Body · 11–12/400', styles.body, 'The law recomputes from sealed evidence.'],
  ['Mono numeral · tnum', `${styles.body} mono num`, '+18.0 bps · 0x7Af3…21bC'],
];

const RADII = [['badge · 4px', '--radius-badge'], ['control · 6px', '--radius-control'], ['panel · 8px', '--radius-panel']];
const CHIP_STATES: CheckStatus[] = ['pass', 'fail', 'pending', 'not_applicable'];

export function DesignSystemScreen() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Design System</h1>
      <p className={styles.body}>Direction A — dark terminal (default) and Direction B — light SaaS (CON-001). The living reference and source of truth for the shared component library.</p>

      <section className={styles.section}>
        <h2 className={styles.heading}>Colors — Direction A (dark terminal)</h2>
        <div className={styles.swatches}>
          {SWATCHES.map(([name, token]) => (
            <div key={token} className={styles.swatch}>
              <span className={styles.chipColor} style={{ background: `var(${token})` }} />
              <span className={`${styles.swatchLabel} mono`}>{name}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Colors — Direction B (light SaaS)</h2>
        {/* data-direction="b" scopes the [data-direction='b'] token overrides to this subtree only. */}
        <div className={styles.swatches} data-direction="b" data-testid="swatches-b">
          {SWATCHES.map(([name, token]) => (
            <div key={token} className={styles.swatch}>
              <span className={styles.chipColor} style={{ background: `var(${token})` }} />
              <span className={`${styles.swatchLabel} mono`}>{name}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Typography</h2>
        <div className={styles.specimens}>
          {TYPE_SPECIMENS.map(([label, cls, sample]) => (
            <div key={label} className={styles.specimenRow}>
              <span className={`${styles.specLabel} mono`}>{label}</span>
              <span className={cls}>{sample}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Spacing &amp; Radius</h2>
        <div className={styles.radii}>
          {RADII.map(([label, token]) => (
            <div key={token} className={styles.radiusBox} style={{ borderRadius: `var(${token})` }}>
              <span className={`${styles.specLabel} mono`}>{label}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Status Badges</h2>
        <div className={styles.badges} data-testid="badge-gallery">
          {BADGE_VARIANTS.map((v) => (
            <span key={v} data-testid={`badge-${v}`}><Badge variant={v} /></span>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Proof-Check Chips</h2>
        <div className={styles.chips} data-testid="proof-chips">
          {CHIP_STATES.map((s) => (
            <div key={s} className={styles.chipCell}>
              <ProofCheckChip status={s} />
              <span className={`${styles.specLabel} mono`}>{s}</span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.heading}>Table</h2>
        <table className={styles.table}>
          <thead>
            <tr><th>#</th><th>AGENT</th><th className={styles.numH}>AVG CLV</th><th>PROOF</th></tr>
          </thead>
          <tbody>
            <tr><td className="mono">1</td><td>value_clv</td><td className="num"><span className={styles.pos}>+18.0</span></td><td><Badge variant="reproducible" /></td></tr>
            <tr><td className="mono">2</td><td>momentum_fr</td><td className="num"><span className={styles.neg}>-4.2</span></td><td><Badge variant="verified" /></td></tr>
          </tbody>
        </table>
      </section>
    </div>
  );
}
