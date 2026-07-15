import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
import type { MakerArenaResultView } from '@/lib/contracts';
import styles from './MakerProofCardScreen.module.css';

// Maker Proof Card (MM-R1) — a deep-link route reached from any maker row's PROOF → (Leaderboard
// / Agents / Duel), mirroring ProofCardScreen. SEC-005: reads ONLY MakerArenaResultView — never
// ProofArtifact / adaptProofArtifact. Leads with the falsification verdict + CI (the claim), not
// a mean; always shows the n=18 small-sample caveat; MM-R1 only — no invented R1.5/R2 UI.
export function MakerProofCardScreen({ result, agentId }: { result: MakerArenaResultView; agentId: string }) {
  const card = result.proof_card;
  const separated = card.falsification.verdict === 'SEPARATED';

  return (
    <article className={styles.proof}>
      <header className={styles.header}>
        <div>
          <Link href="/leaderboard?lane=maker" className={styles.back}>← Maker leaderboard</Link>
          <div className={styles.titleRow}>
            <h1 className={styles.title}>Maker Proof Card</h1>
            <Badge variant="mm-r1" />
            <span className={`${styles.meta} mono`}>{agentId} · MM-R1</span>
          </div>
          <div className={`${styles.subMeta} mono`}>maker-arena-v1 · forward-markout quote-quality falsification · {result.source_mode}</div>
        </div>
        <Badge variant="small-n">n={card.n_fixtures} · SMALL SAMPLE</Badge>
      </header>

      {/* LEAD: the falsification claim — never buried under the mean. */}
      <section className={styles.lead} aria-label="Falsification result">
        <div className={styles.leadHead}>
          <span className={styles.sectionLabel}>FALSIFICATION RESULT</span>
          <InfoTip label={GLOSSARY.falsification.label}>{GLOSSARY.falsification.definition}</InfoTip>
          <span className={styles.leadNote}>the claim · lead with this</span>
        </div>
        <div className={styles.leadBody}>
          <div className={styles.leadClaim}>
            <span data-testid="maker-proof-verdict"><Badge variant={separated ? 'separated' : 'inconclusive'} /></span>
            <div>
              <div className={styles.leadHeadline}>TxLINE-fair separated from the naive control on quote quality</div>
              <div className={styles.leadSub}>{card.falsification.headline} · whole CI above zero</div>
            </div>
          </div>
          <div className={styles.leadStats}>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Δ DELTA</span>
              <span data-testid="maker-proof-delta"><Num value={card.falsification.delta_bps} kind="bps" /></span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>95% CI</span>
              <span className="mono" data-testid="maker-proof-ci">[{card.falsification.ci_low_bps}, {card.falsification.ci_high_bps}]</span>
            </div>
          </div>
        </div>
      </section>

      <div className={styles.grid}>
        <section className={styles.panel} aria-label="Universe">
          <div className={styles.panelHead}><span className={styles.sectionLabel}>UNIVERSE</span></div>
          <div className={styles.panelBody} data-testid="maker-proof-universe">
            <div className={styles.bigStat}><span className={styles.bigNum}>{card.n_fixtures}</span><span className={styles.bigUnit}>fixtures scored</span></div>
            <p className={styles.note}>{card.small_n_note}</p>
          </div>
        </section>

        <section className={styles.panel} aria-label="Window markout aggregate">
          <div className={styles.panelHead}>
            <span className={styles.sectionLabel}>WINDOW MARKOUT AGGREGATE</span>
            <Badge variant="inverted">NOT CLV</Badge>
          </div>
          <div className={styles.panelBody}>
            <div className={styles.statRow}>
              <div className={styles.stat}><span className={styles.statLabel}>WINDOW MARKOUT</span><span className="mono">{card.window_clv_analog.window_markout_bps} bps</span></div>
              <div className={styles.stat}><span className={styles.statLabel}>ACTIONS</span><span className="mono">{card.window_clv_analog.window_action_count.toLocaleString()}</span></div>
            </div>
            <p className={styles.note}>{card.window_clv_analog.note}</p>
          </div>
        </section>
      </div>

      <section className={styles.panel} aria-label="Caveats">
        <div className={styles.panelHead}><span className={styles.sectionLabel}>CAVEATS</span></div>
        <div className={styles.caveats}>
          <div className={styles.caveatRow} data-testid="maker-proof-trades-caveat">
            <Badge variant="trades-not-fills" />
            <p>{card.trades_not_fills_caveat ?? "Observed trades are not the maker's own fills — no fill attribution at MM-R1."}</p>
          </div>
          <div className={styles.caveatRow} data-testid="maker-proof-edge-caveat">
            <Badge variant="inverted">NO EDGE CLAIM</Badge>
            <p>No executable-edge / PnL / fill claim — <span className="mono">real_executable_edge_bps</span> is <span className="mono">null by construction</span>.</p>
          </div>
          <div className={styles.caveatRow}>
            <Badge variant="mm-r1">DIAGNOSTIC</Badge>
            <p>Mean markout is geometry (≈ half-spread/ref), not quote quality — never the rank axis. Rank is toxicity loss, lower is better.</p>
          </div>
        </div>
      </section>

      {/* Honest-empty future rungs (MM-R1 only — no invented R1.5/R2 UI). */}
      <div className={styles.futureGrid}>
        <div className={styles.futureCard} data-testid="maker-proof-empty-rung">
          <div className={styles.futureTitle}>Trade-aware diagnostic</div>
          <div className={styles.futureNote}>future · operator-gated (real on-chain trade capture not run)</div>
        </div>
        <div className={styles.futureCard} data-testid="maker-proof-empty-rung">
          <div className={styles.futureTitle}>Fill-assumption bracket</div>
          <div className={styles.futureNote}>not present at MM-R1</div>
        </div>
        <div className={styles.futureCard} data-testid="maker-proof-empty-rung">
          <div className={styles.futureTitle}>Anchor / verify</div>
          <div className={styles.futureNote}>not yet surfaced by the API</div>
        </div>
      </div>
    </article>
  );
}
