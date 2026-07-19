import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
import { shortHash } from '@/lib/format';
import { MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { deriveMakerVerdict, deriveCounterfactualCapacity } from '@/lib/makerVerdict';
import type { MakerArenaResultView } from '@/lib/contracts';
import styles from './MakerProofCardScreen.module.css';

// Maker Proof Card (MM-R1) — a deep-link route reached from any maker row's PROOF → (Leaderboard
// / Agents / Duel), mirroring ProofCardScreen. SEC-005: reads ONLY MakerArenaResultView — never
// ProofArtifact / adaptProofArtifact. Leads with the three-state falsification verdict + CI (the
// claim, derived via deriveMakerVerdict — never a boolean shortcut, I-R M1), not a mean; carries
// the sealed config_hash identity in the header (I-R M3); always shows the n=18 small-sample
// caveat; MM-R1 only — no invented R1.5/R2 UI.
export function MakerProofCardScreen({ result, agentId }: { result: MakerArenaResultView; agentId: string }) {
  const card = result.proof_card;
  const candidate = result.leaderboard.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'candidate') ?? result.leaderboard[0];
  const control = result.leaderboard.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'control') ?? result.leaderboard[1];
  const verdict = deriveMakerVerdict(card.falsification, {
    candidate: candidate.agent_id, control: control.agent_id,
  });
  // AC-30/AC-31: a historical entry/exit capacity claim (when present) is ALWAYS a bounded, LABELED
  // counterfactual — a fourth evidence class, kept structurally distinct from the sealed arena score,
  // the null exec-edge, and the falsification claim. Absent on the sealed MM-R1 fixture ⇒ null ⇒ the
  // honest-empty future rungs render exactly as before.
  const cf = result.historical_capacity ? deriveCounterfactualCapacity(result.historical_capacity) : null;

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
          <div className={`${styles.subMeta} mono`}>
            maker-arena-v1 · forward-markout quote-quality falsification · {result.source_mode} · config{' '}
            <span title={result.config_hash} data-testid="maker-proof-config-hash">{shortHash(result.config_hash)}</span>
            <InfoTip label={GLOSSARY.maker_config_hash.label}>{GLOSSARY.maker_config_hash.definition}</InfoTip>
          </div>
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
            <span data-testid="maker-proof-verdict"><Badge variant={verdict.badge}>{verdict.badgeText}</Badge></span>
            <div>
              <div className={styles.leadHeadline}>{verdict.headline}</div>
              <div className={styles.leadSub}>{card.falsification.headline} · {verdict.ciSub}</div>
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

      {/* AC-30/AC-31 — the fourth evidence class. Rendered ONLY when a historical capacity claim is
          carried; it is a bounded, labeled counterfactual, deliberately OUTSIDE the rank/edge/fill
          surfaces above so no class is ever conflated. A third-party print or counterfactual ceiling
          can never be presented as our own fill, PnL, receipt, or rank. */}
      {cf && (
        <section
          className={styles.panel}
          aria-label="Historical capacity — counterfactual"
          data-testid="maker-proof-counterfactual"
          data-evidence-class={cf.badge}
        >
          <div className={styles.panelHead}>
            <span className={styles.sectionLabel}>HISTORICAL CAPACITY</span>
            <Badge variant={cf.badge} />
            <InfoTip label={GLOSSARY.counterfactual_capacity.label}>{GLOSSARY.counterfactual_capacity.definition}</InfoTip>
          </div>
          <div className={styles.panelBody}>
            <div className={styles.statRow}>
              <div className={styles.stat}>
                <span className={styles.statLabel}>BOUNDED CEILING</span>
                <span className="mono" data-testid="maker-proof-counterfactual-ceiling">
                  ${cf.boundedCapacityUsd.toLocaleString()}{cf.isBounded ? ' · clamped to observed liquidity' : ''}
                </span>
              </div>
            </div>
            <p className={styles.note}>{cf.label} — bounded by matched observed liquidity, never routed to fill / PnL / rank / executable edge.</p>
          </div>
        </section>
      )}

      {/* QuoteGuard behavior ablation entry (F-8) — a contextual deep-link to the guard OFF vs ON
          BEHAVIOR comparison. It is not a rank/profit surface; the link is keyed by the card's
          identity and honestly resolves to "no recorded ablation" until one exists for the instance. */}
      <Link href={`/proof/maker-ablation/${agentId}`} className={styles.ablationEntry} data-testid="maker-proof-ablation-entry">
        <div className={styles.ablationText}>
          <div className={styles.ablationTitleRow}>
            <span className={styles.ablationTitle}>QuoteGuard behavior ablation</span>
            <Badge variant="behavior-ablation">BEHAVIOR ABLATION</Badge>
          </div>
          <span className={styles.ablationBlurb}>same strategy &amp; tape, guard OFF vs ON — behavior only, not rank or profit</span>
        </div>
        <span className={styles.ablationOpen}>OPEN →</span>
      </Link>

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
