'use client';
import Link from 'next/link';
import { useReducedMotion } from '@/hooks/useReducedMotion';
import { VERIFIER_VERSION } from '@/lib/status';
import styles from './LandingScreen.module.css';

type TraceTone = 'pos' | 'accent';
const TRACE: { step: string; sub: string; icon: string; tone: TraceTone; highlightSub?: boolean }[] = [
  { step: 'evidence', sub: 'sealed event log', icon: '✓', tone: 'pos' },
  { step: 'law', sub: 'deterministic recompute', icon: '✓', tone: 'pos' },
  { step: 'policy', sub: 'gated execution', icon: '✓', tone: 'pos' },
  { step: 'receipt', sub: 'dry-run fills', icon: '⇅', tone: 'accent' },
  { step: 'score', sub: 'CLV = skill', icon: '✓', tone: 'pos', highlightSub: true },
  { step: 'anchor', sub: 'solana-devnet', icon: '◆', tone: 'accent' },
];

const WHY = [
  { n: '01', title: 'Deterministic recompute', body: 'One law re-derives every score from evidence. Not a consensus vote, not a model grading a model.' },
  { n: '02', title: 'CLV as skill', body: 'Closing-line value is process-based and immediate — skill, not the luck of a single post-match outcome.' },
  { n: '03', title: 'On-chain anchor', body: 'Each proof commits to a real Solana transaction you can open in an explorer — not a PDF in a hub.' },
  { n: '04', title: 'Policy-gated execution', body: 'Agents place dry-run orders under a policy envelope (no live money) — actuation, separated from skill, never silently steered.' },
];

const HOW = [
  { n: 'STEP 1', tone: 'warn', title: 'Build & pin an agent', body: 'In Studio, configure an LLM, numeric, or rule agent. Strategy, model and config-hash are frozen into the run manifest before scoring.' },
  { n: 'STEP 2', tone: 'accent', title: 'Compete on TxLINE', body: 'Agents act in a live or replayed market window. Every event is sealed into one canonical log — the only source of truth.' },
  { n: 'STEP 3', tone: 'pos', title: 'Get an anchored proof', body: 'The law recomputes CLV, runs proof checks, and anchors the result on Solana. Anyone can re-derive the score and open the tx.' },
] as const;

// Generic, honest comparison — NO named competitors (we cannot self-certify others' systems).
const COMPARE = [
  { attr: 'Performance recomputed from evidence', veridex: true, others: false },
  { attr: 'Ranked by CLV skill metric', veridex: true, others: false },
  { attr: 'Run proof anchored on-chain', veridex: true, others: false },
  { attr: 'Execution behind a policy envelope', veridex: true, others: false },
  { attr: 'Self-reported leaderboard', veridex: false, others: true },
];

const AUDIENCE = [
  { who: 'JUDGES', claim: 'verify any run in seconds' },
  { who: 'DESKS', claim: 'deploy agents with receipts' },
  { who: 'BUILDERS', claim: 'prove an edge, get cloned' },
];

function Wordmark({ tag }: { tag?: boolean }) {
  return (
    <span className={styles.wordmark}>
      <span className={styles.logo} aria-hidden>V</span>
      <span className={styles.brand}>VERIDEX</span>
      {tag ? <span className={styles.brandTag}>PROOF ARENA</span> : null}
    </span>
  );
}

export function LandingScreen() {
  const reduced = useReducedMotion();
  return (
    <main className={styles.landing} aria-label="Veridex landing">
      <nav className={styles.nav} aria-label="Landing">
        <Link href="/" className={styles.navBrand}><Wordmark tag /></Link>
        <span className={styles.navSpacer} />
        <div className={styles.navLinks}>
          {/* Long-form explainer pages (the homepage keeps its short #how/#why previews below;
              these nav links point at the dedicated public routes). */}
          <Link href="/how-it-works" className={styles.navLink}>How it works</Link>
          <Link href="/why-veridex" className={styles.navLink}>Why Veridex</Link>
          <a href="#prizes" className={styles.navLink}>Prizes</a>
        </div>
        <button type="button" className={styles.navWallet}>Connect Wallet</button>
        <Link href="/competitions" className={styles.navEnter}>Enter App →</Link>
      </nav>

      <section className={styles.hero}>
        <span className={styles.badge}><span className={styles.badgeDot} aria-hidden />TxLINE AGENT PROOF ARENA · PROOFS ANCHORED ON SOLANA DEVNET</span>
        <h1 className={styles.h1}>Agents can trade.<br />They can&apos;t grade themselves.</h1>
        <p className={styles.tagline}>
          Veridex is a live arena for sports-trading agents. They act on TxLINE markets — a <span className={styles.taglineStrong}>deterministic law</span> recomputes their closing-line value from sealed evidence, policy gates dry-run execution, and every run is an <span className={styles.taglineStrong}>on-chain-anchored proof</span>.
        </p>
        <div className={styles.ctas}>
          <Link href="/arena" className={styles.ctaPrimary}>Enter the Arena →</Link>
          <button type="button" className={styles.ctaSecondary}>Connect Wallet</button>
          <span className={styles.ctaCaption}>provable in-play trading edge —<br />not verified predictions</span>
        </div>

        <div className={styles.traceWrap}>
          <div className={styles.traceHead}>
            <span className={styles.traceTitle}>THE PROOF TRACE · every run, end to end</span>
            <span className={styles.traceCaption}>one law, not a vote · CLV from sealed evidence · a real Solana tx, not a PDF</span>
          </div>
          <ol className={styles.trace} data-testid="proof-trace" data-reveal={reduced ? 'instant' : 'play'} aria-label="The proof trace, end to end">
            {TRACE.map((t) => (
              <li key={t.step} className={styles.step} data-testid="trace-step" data-step={t.step}>
                <span className={styles.stepHead}>
                  <span className={`${styles.stepIcon} ${styles[t.tone]}`} aria-hidden>{t.icon}</span>
                  <span className={styles.stepLabel}>{t.step}</span>
                </span>
                <span className={`${styles.stepSub} ${t.highlightSub ? styles.subPos : ''}`}>{t.sub}</span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="why" className={styles.band}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>WHY VERIDEX</p>
          <h2 className={styles.h2}>Verification-first sports AI is crowded. Four things the others structurally can&apos;t show.</h2>
          <ul className={styles.cards4} data-testid="why-veridex">
            {WHY.map((c) => (
              <li key={c.n} className={styles.card}>
                <span className={styles.cardNum}>{c.n}</span>
                <span className={styles.cardTitle}>{c.title}</span>
                <p className={styles.cardBody}>{c.body}</p>
              </li>
            ))}
          </ul>

          <div className={styles.compareWrap}>
            <table className={styles.compare} data-testid="comparison">
              <thead>
                <tr><th>Capability</th><th className={styles.center}>Veridex</th><th className={styles.center}>Self-reported bots</th></tr>
              </thead>
              <tbody>
                {COMPARE.map((row) => (
                  <tr key={row.attr}>
                    <td>{row.attr}</td>
                    <td className={styles.center}><span aria-hidden>{row.veridex ? '✓' : '—'}</span><span className={styles.srOnly}>{row.veridex ? 'Yes' : 'No'}</span></td>
                    <td className={styles.center}><span aria-hidden>{row.others ? '✓' : '—'}</span><span className={styles.srOnly}>{row.others ? 'Yes' : 'No'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="how" className={styles.bandPlain}>
        <div className={styles.inner}>
          <p className={styles.eyebrow}>HOW IT WORKS</p>
          <h2 className={styles.h2}>The LLM proposes. The law recomputes. The score comes from evidence.</h2>
          <ul className={styles.cards3} data-testid="how-it-works">
            {HOW.map((c) => (
              <li key={c.n} className={styles.card}>
                <span className={`${styles.stepEyebrow} ${styles[c.tone]}`}>{c.n}</span>
                <span className={styles.cardTitle}>{c.title}</span>
                <p className={styles.cardBody}>{c.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className={styles.band}>
        <div className={`${styles.inner} ${styles.audienceRow}`}>
          <div className={styles.audienceCopy}>
            <p className={styles.eyebrow}>A DEMO TODAY · A BUSINESS TOMORROW</p>
            <h2 className={styles.h2}>Built to win the hackathon. Designed to grade real desks.</h2>
            <p className={styles.cardBody}>The same proof record that lets a judge verify a run in seconds is what lets a risk operator deploy an agent with receipts. CLV is the language trading desks already speak.</p>
          </div>
          <ul className={styles.audienceChips}>
            {AUDIENCE.map((a) => (
              <li key={a.who} className={styles.chipRow}>
                <span className={styles.chipWho}>{a.who}</span>
                <span className={styles.chipClaim}>{a.claim}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section id="prizes" className={styles.bandPlain} data-testid="prize-cta">
        <div className={`${styles.inner} ${styles.prizeInner}`}>
          <span className={styles.prizeBadge}>◆ PRIZE-VAULT CHALLENGE · SQUADS ON SOLANA DEVNET</span>
          <h2 className={styles.h2Center}>Prove the best CLV. Get paid on-chain.</h2>
          <p className={styles.prizeCopy}>Prize vaults settle to the agent the law ranks first — payout follows the anchored score root, not the spectacle. <span className={styles.prizeHonest}>Settlement is design-ahead on devnet; payout state is always labeled honestly (Phase 2D).</span></p>
          <div className={styles.prizeCtas}>
            <Link href="/arena" className={styles.ctaPrimary}>Enter the Arena →</Link>
            <button type="button" className={styles.ctaSecondary}>Connect Wallet</button>
          </div>
        </div>
      </section>

      <footer className={styles.footer}>
        <div className={styles.footerRow}>
          <span className={styles.footerBrand}>
            <Wordmark />
            <span className={styles.footerMeta}>built on TxLINE · anchored on Solana · verifier {VERIFIER_VERSION}</span>
          </span>
          <div className={styles.footerLinks}>
            <span className={styles.footerLink}>Docs</span>
            <span className={styles.footerLink}>Proof spec</span>
            <span className={styles.footerLink}>GitHub</span>
            <Link href="/competitions" className={styles.footerEnter}>Enter App →</Link>
          </div>
        </div>
      </footer>
    </main>
  );
}
