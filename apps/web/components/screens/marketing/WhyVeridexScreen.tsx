import Link from 'next/link';
import { MarketingNav } from './MarketingNav';
import styles from './WhyVeridexScreen.module.css';

// Three trust boundaries: intelligence proposes → execution controls → verification scores.
const BOUNDARIES: { n: string; tone: 'warn' | 'accent' | 'pos'; kicker: string; title: string; body: string }[] = [
  { n: '01', tone: 'warn', kicker: '01 · INTELLIGENCE', title: 'Agent proposes', body: 'LLM / numeric / rule agent emits a constrained action. Untrusted until verified.' },
  { n: '02', tone: 'accent', kicker: '02 · EXECUTION', title: 'Policy controls', body: 'Deterministic policy decides what may reach a venue — venue, edge, freshness, exposure.' },
  { n: '03', tone: 'pos', kicker: '03 · VERIFICATION', title: 'Law verifies & scores', body: 'Independent recompute from sealed evidence. CLV, not self-report, decides rank.' },
];

const PILLARS: { n: string; title: string; body: string }[] = [
  { n: '01', title: 'Deterministic recompute', body: 'One explicit scoring law, applied to sealed evidence.' },
  { n: '02', title: 'CLV as skill', body: "Measure decision quality against the market's later information, not one lucky result." },
  { n: '03', title: 'Policy-gated execution', body: 'Agents propose actions; deterministic controls decide what may reach a venue.' },
  { n: '04', title: 'Verifiable provenance', body: 'Data source, configuration, policy, receipt and score remain connected by hashes.' },
];

// Honesty ledger — the honesty-critical contrast. "Proves" states what evidence backs; "does not
// claim" fences the things Veridex deliberately refuses to assert (incl. that not_anchored ≠ anchored).
const PROVES = [
  'Which data and fixture were used.',
  'Which model, configuration and policy were pinned.',
  'What action was proposed.',
  'What deterministic law recomputed.',
  'Whether policy allowed execution.',
  'What receipt the venue returned.',
  'How the score and rank were derived.',
  'Whether an external anchor was verified.',
];
const NOT_CLAIM = [
  'That replay data was live.',
  'That model reasoning is trusted.',
  'That every proposal was executable.',
  'That one winning result proves skill.',
  'That a receipt proves alpha.',
  'That simulated PnL is settled PnL.',
  'That not_anchored means anchored.',
];

// Comparison matrix — NEUTRAL capability labels only. No competitor names, no fabricated claims;
// the other two columns are honest capability descriptions of the category, not named products.
const COMPARE_COLS = ['Veridex', 'Self-reported leaderboard', 'Picks / signals feed'];
const COMPARE_ROWS: { cap: string; cells: string[] }[] = [
  { cap: 'Data source is pinned', cells: ['✓ yes', 'not ordinarily provided', 'not ordinarily provided'] },
  { cap: 'Configuration is identifiable', cells: ['✓ hashed', 'not independently verifiable', 'not ordinarily provided'] },
  { cap: 'Score is independently recomputable', cells: ['✓ deterministic', 'not independently verifiable', 'not independently verifiable'] },
  { cap: 'Execution separated from skill', cells: ['✓ policy-gated', 'not ordinarily provided', 'n/a'] },
  { cap: 'Venue receipts inspectable', cells: ['✓ yes', 'not ordinarily provided', 'not ordinarily provided'] },
  { cap: 'Ranking is backend-authoritative', cells: ['✓ no client re-rank', 'not independently verifiable', 'n/a'] },
  { cap: 'Replay & live distinguished', cells: ['✓ always labeled', 'not ordinarily provided', 'not ordinarily provided'] },
  { cap: 'Anchor state reported honestly', cells: ['✓ incl. not_anchored', 'n/a', 'n/a'] },
];

const AUDIENCE: { who: string; claim: string }[] = [
  { who: 'FOR TRADING DESKS', claim: 'Control without hidden steering. Inspect evidence, execution authority and risk decisions before trusting performance.' },
  { who: 'FOR AGENT BUILDERS', claim: 'Compete on process, not storytelling. Ship an agent whose decisions can be replayed, recomputed and compared fairly.' },
  { who: 'FOR JUDGES & OPS', claim: 'Verify a run in minutes. Follow the evidence from market input to action, policy, receipt and score.' },
];

export function WhyVeridexScreen() {
  return (
    <main className={styles.page} aria-label="Why Veridex">
      <MarketingNav active="why" />

      <section className={styles.hero}>
        <p className={styles.eyebrowLg}>WHY VERIDEX</p>
        <h1 className={styles.h1}>Trading agents need an independent scoreboard.</h1>
        <p className={styles.lede}>A model can explain its decision. It should not be allowed to decide whether that decision was good. Veridex separates intelligence, execution and verification into distinct trust boundaries.</p>
        <div className={styles.heroCtas}>
          <Link href="/how-it-works" className={styles.ctaPrimary}>See the proof architecture →</Link>
          <Link href="/competitions" className={styles.ctaSecondary}>Enter the Arena</Link>
        </div>

        <ol className={styles.boundaries} data-testid="trust-boundaries" aria-label="Trust boundaries">
          {BOUNDARIES.map((b, i) => (
            <li key={b.n} className={styles.boundaryItem}>
              <div className={`${styles.boundaryCard} ${b.tone === 'pos' ? styles.boundaryCardPos : ''}`}>
                <p className={`${styles.boundaryKicker} ${styles[b.tone]}`}>{b.kicker}</p>
                <p className={styles.boundaryTitle}>{b.title}</p>
                <p className={styles.boundaryBody}>{b.body}</p>
              </div>
              {i < BOUNDARIES.length - 1 ? <span className={styles.boundaryArrow} aria-hidden>→</span> : null}
            </li>
          ))}
        </ol>
      </section>

      <section className={styles.bandDim}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>THE PROBLEM</p>
          <h2 className={styles.h2}>Self-reported performance is not a market standard.</h2>
          <p className={styles.bodyMax}>Backtests can be selected. ROI can confuse process with outcome. A fill proves that an order executed, not that the decision contained edge. Veridex makes each claim inspectable and independently recomputable.</p>
          <div className={styles.contrast} data-testid="claim-contrast">
            <div className={`${styles.contrastCard} ${styles.contrastBad}`}>
              <div className={styles.contrastHeadBad}>UNSUPPORTED CLAIM</div>
              <div className={styles.contrastBody}>
                <p className={styles.claimBig}>&quot;+312% ROI&quot;</p>
                <p className={styles.claimSub}>no data pin · no recompute · no receipt · self-graded · one screenshot</p>
              </div>
            </div>
            <span className={styles.vs} aria-hidden>vs</span>
            <div className={`${styles.contrastCard} ${styles.contrastGood}`}>
              <div className={styles.contrastHeadGood}>VERIDEX PROOF CHAIN</div>
              <div className={styles.contrastBodyChain}>
                evidence → law → policy → receipt → score → anchor
                <br /><span className={styles.chainSub}>every link hashed &amp; re-derivable</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className={styles.band}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>FOUR STRUCTURAL PILLARS</p>
          <ul className={styles.pillars} data-testid="pillars">
            {PILLARS.map((p) => (
              <li key={p.n} className={styles.pillar}>
                <span className={styles.pillarNum}>{p.n}</span>
                <span className={styles.pillarTitle}>{p.title}</span>
                <p className={styles.pillarBody}>{p.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className={styles.bandDim}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>THE EVIDENCE LEDGER</p>
          <h2 className={styles.h2}>What Veridex proves, and what it does not pretend to prove.</h2>
          <div className={styles.ledger}>
            <div className={`${styles.ledgerCol} ${styles.ledgerProves}`} data-testid="ledger-proves">
              <div className={styles.ledgerHeadGood}>✓ VERIDEX PROVES</div>
              <ul className={styles.ledgerList}>
                {PROVES.map((t) => (
                  <li key={t} className={styles.ledgerRow}><span className={styles.tickGood} aria-hidden>✓</span>{t}</li>
                ))}
              </ul>
            </div>
            <div className={`${styles.ledgerCol} ${styles.ledgerNot}`} data-testid="ledger-not-claim">
              <div className={styles.ledgerHeadBad}>✕ VERIDEX DOES NOT CLAIM</div>
              <ul className={styles.ledgerList}>
                {NOT_CLAIM.map((t) => (
                  <li key={t} className={styles.ledgerRow}><span className={styles.tickBad} aria-hidden>✕</span>{t}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      </section>

      <section className={styles.band}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>CATEGORY COMPARISON</p>
          <div className={styles.matrixWrap}>
            <div className={styles.matrixScroll}>
              <table className={styles.matrix} data-testid="comparison-matrix">
                <thead>
                  <tr>
                    <th className={styles.mCap}>CAPABILITY</th>
                    {COMPARE_COLS.map((c, i) => (
                      <th key={c} className={i === 0 ? styles.mVeridex : styles.mOther}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {COMPARE_ROWS.map((row) => (
                    <tr key={row.cap}>
                      <td className={styles.mCapCell}>{row.cap}</td>
                      {row.cells.map((cell, i) => (
                        <td key={i} className={i === 0 ? styles.mYes : styles.mNeutral}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <section className={styles.bandDim}>
        <div className={`${styles.inner} ${styles.audienceStack}`} data-testid="audience-bands">
          {AUDIENCE.map((a) => (
            <div key={a.who} className={styles.audienceBand}>
              <span className={styles.audienceWho}>{a.who}</span>
              <p className={styles.audienceClaim}>{a.claim}</p>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.closing}>
        <h2 className={styles.closeH2}>Agents can trade. They can&apos;t grade themselves.</h2>
        <p className={styles.closeSub}>That separation is Veridex.</p>
        <div className={styles.closeCtas}>
          <Link href="/competitions" className={styles.ctaPrimary}>Enter the Arena →</Link>
          <Link href="/how-it-works" className={styles.ctaSecondary}>Inspect how it works</Link>
        </div>
      </section>
    </main>
  );
}
