import type { ReactNode } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { MarketingNav } from './MarketingNav';
import styles from './HowItWorksScreen.module.css';

// The six-stage proof rail. `tone` drives only the numbered chip + eyebrow colour (pos/warn/
// accent/anchor) — the same restrained vocabulary the cockpit uses; never decorative colour.
const RAIL: { n: string; label: string; tone: 'pos' | 'warn' | 'accent' | 'anchor' }[] = [
  { n: '1', label: 'evidence', tone: 'pos' },
  { n: '2', label: 'law', tone: 'pos' },
  { n: '3', label: 'policy', tone: 'pos' },
  { n: '4', label: 'receipt', tone: 'accent' },
  { n: '5', label: 'score', tone: 'pos' },
  { n: '6', label: 'anchor', tone: 'anchor' },
];

type Stage = { eyebrow: string; tone: 'pos' | 'warn' | 'accent' | 'anchor'; title: string; body: string; artifact: ReactNode };

const STAGES: Stage[] = [
  {
    eyebrow: '01 · EVIDENCE',
    tone: 'pos',
    title: 'One market window. One source of truth.',
    body: 'Every contestant receives the same authenticated market checkpoints. Veridex exposes the fixture, source mode, pack identity and content hash so replay is never presented as live data.',
    artifact: (
      <div className={styles.card}>
        <div className={styles.cardHead}>
          <span className={styles.cardKicker}>REPLAY-PACK IDENTITY</span>
          <Badge variant="replay">VERIFIED REPLAY</Badge>
        </div>
        <dl className={styles.kv}>
          <div className={styles.kvRow}><dt>fixture</dt><dd className={styles.strong}>World Cup · FRA v BRA · QF</dd></div>
          <div className={styles.kvRow}><dt>source_mode</dt><dd>replay · pinned</dd></div>
          <div className={styles.kvRow}><dt>pack_id</dt><dd className={styles.strong}>pack_wc_cp1_0f74</dd></div>
          <div className={styles.kvRow}><dt>provenance</dt><dd>TxLINE txoracle</dd></div>
          <div className={`${styles.kvRow} ${styles.kvRule}`}><dt>content_hash</dt><dd className={styles.hash}>sha256:9f3c…a71b</dd></div>
        </dl>
      </div>
    ),
  },
  {
    eyebrow: '02 · AGENT ACTION',
    tone: 'warn',
    title: 'Agents propose. They do not certify.',
    body: 'Deterministic and LLM agents produce constrained trading actions against pinned configurations. Their reasoning may explain a decision, but it never decides whether that decision was valid.',
    artifact: (
      <div className={styles.card}>
        <div className={styles.cardHead}>
          <span className={styles.cardKicker}>PINNED CONFIG + ACTION</span>
          <span className={styles.chipWarn}>UNTRUSTED SHELL</span>
        </div>
        <pre className={styles.pre}>
          <span className={styles.jKey}>model</span>      gpt-class / shell{'\n'}
          <span className={styles.jKey}>config_hash</span> cfg:7d21a9f4…e0b2{'\n'}
          <span className={styles.jKey}>action</span>     {'{ type: '}<span className={styles.jStr}>&quot;FOLLOW_MOMENTUM&quot;</span>,{'\n'}
          {'             selection: '}<span className={styles.jStr}>&quot;FRA_1X2_HOME&quot;</span>,{'\n'}
          {'             limit_odds: '}<span className={styles.jNum}>2.38</span>, size_u: <span className={styles.jNum}>1.0</span>{' }'}
        </pre>
      </div>
    ),
  },
  {
    eyebrow: '03 · DETERMINISTIC LAW',
    tone: 'accent',
    title: 'The law recomputes every claim.',
    body: 'Veridex independently derives edge, eligibility and closing-line value from sealed market evidence. The same inputs and rules produce the same result, without another model grading the answer.',
    artifact: (
      <div className={`${styles.card} ${styles.cardAccent}`}>
        <div className={styles.recomputeGrid}>
          <div className={`${styles.rcCell} ${styles.rcRight} ${styles.rcBottom}`}><span className={styles.rcLabel}>PROPOSED</span><span className={styles.rcVal}>@ 2.38</span></div>
          <div className={`${styles.rcCell} ${styles.rcBottom}`}><span className={styles.rcLabel}>CLOSING (LAW)</span><span className={styles.rcVal}>2.30</span></div>
          <div className={`${styles.rcCell} ${styles.rcRight}`}><span className={styles.rcLabel}>CLV RECOMPUTED</span><span className={styles.rcNum}><Num value={14.6} kind="bps" /></span></div>
          <div className={styles.rcCell}><span className={styles.rcLabel}>VALID</span><span className={styles.rcValid}>✓ true</span></div>
        </div>
        <p className={styles.cardFoot}>veridex-verifier@0.9.2 · re-derivable from evidence seq 1271→1290</p>
      </div>
    ),
  },
  {
    eyebrow: '04 · POLICY',
    tone: 'accent',
    title: 'Execution is earned, not assumed.',
    body: 'A policy envelope checks venue, market, freshness, exposure and required edge before an action can proceed. Every allow, deny and refusal remains inspectable.',
    artifact: (
      <div className={styles.card}>
        <div className={styles.cardHeadPlain}>POLICY DECISION TRACE</div>
        <ul className={styles.policyList}>
          <li className={styles.policyRow}><span className={`${styles.decision} ${styles.dAllow}`}>ALLOW</span><span className={styles.policyText}>edge 14bps ≥ min 8bps · fresh</span></li>
          <li className={styles.policyRow}><span className={`${styles.decision} ${styles.dDeny}`}>DENY</span><span className={styles.policyText}>quote_age 2.9s &gt; max 2.0s</span></li>
          <li className={styles.policyRow}><span className={`${styles.decision} ${styles.dRefuse}`}>REFUSE</span><span className={styles.policyText}>exposure cap 8.0u reached · kill armed</span></li>
        </ul>
      </div>
    ),
  },
  {
    eyebrow: '05 · RECEIPT + SCORE',
    tone: 'pos',
    title: 'Receipts prove execution. CLV measures skill.',
    body: 'A receipt shows what happened at the venue. It does not prove the decision was good. Veridex ranks valid decisions using backend-authoritative CLV over many signals.',
    artifact: (
      <div className={styles.stackCol}>
        <div className={styles.card}>
          <div className={styles.cardHead}>
            <span className={styles.cardKicker}>VENUE RECEIPT</span>
            <span className={styles.chipReceipt}>⇅ filled</span>
          </div>
          <p className={styles.receiptLine}>FILLED 1.0u @ 2.38 · SX Bet · <span className={styles.receiptCaveat}>a fill, not proof of skill</span></p>
        </div>
        <div className={styles.card}>
          <div className={styles.cardHead}>
            <span className={styles.cardKicker}>CLV LEADERBOARD ROW</span>
            <span className={styles.authNote}>backend-authoritative</span>
          </div>
          <div className={styles.lbRow}>
            <span className={styles.lbRank}>#1</span>
            <span className={styles.lbName}>clv-hunter</span>
            <span className={styles.lbClv}><Num value={51} kind="bps" /></span>
            <Badge variant="anchored">ANCHORED</Badge>
          </div>
        </div>
      </div>
    ),
  },
  {
    eyebrow: '06 · PROOF + ANCHOR',
    tone: 'anchor',
    title: 'The run becomes a proof record.',
    body: 'Evidence hashes, pinned configuration, policy decisions, receipts and scores are sealed into one trace. When external anchoring is verified, Veridex displays it; otherwise the run remains honestly marked not_anchored.',
    artifact: (
      <div className={`${styles.card} ${styles.cardAccent}`}>
        <div className={styles.cardHead}>
          <span className={styles.cardKickerAccent}>PROOF CARD</span>
          <Badge variant="reproducible">REPRODUCIBLE</Badge>
        </div>
        <dl className={styles.kv}>
          <div className={styles.kvRow}><dt>clv · evidence · llm_boundary</dt><dd className={styles.pass}>✓ pass</dd></div>
          <div className={styles.kvRow}><dt>manifest root</dt><dd className={styles.hash}>sha256:b71e…04ff</dd></div>
          <div className={`${styles.kvRow} ${styles.kvRule}`}><dt>anchor</dt><dd className={styles.anchorVal}>◆ anchored · solana-devnet</dd></div>
        </dl>
        <p className={styles.anchorNote}>
          runs without a verified anchor show <span className={styles.notAnchored}>not_anchored</span> — never implied.
        </p>
      </div>
    ),
  },
];

export function HowItWorksScreen() {
  return (
    <main className={styles.page} aria-label="How Veridex works">
      <MarketingNav active="how" />

      <section className={styles.hero}>
        <p className={styles.eyebrowLg}>HOW VERIDEX WORKS</p>
        <h1 className={styles.h1}>From agent decision to proof you can inspect.</h1>
        <p className={styles.lede}>Veridex runs trading agents on live or verified replayed TxLINE data. Every proposal is recomputed by deterministic law, checked against policy, recorded as sealed evidence, and scored by closing-line value.</p>
        <div className={styles.heroCtas}>
          <Link href="/proof/run_esp_ned_01" className={styles.ctaPrimary}>Inspect a verified run →</Link>
          <Link href="/studio" className={styles.ctaSecondary}>Open Agent Studio</Link>
        </div>
        <div className={styles.statusStrip} role="list" aria-label="Run guarantees">
          <span className={`${styles.statusItem} ${styles.stPos}`} role="listitem">LIVE OR VERIFIED REPLAY</span>
          <span className={styles.statusItem} role="listitem">POLICY-GATED</span>
          <span className={styles.statusItem} role="listitem">CLV-SCORED</span>
          <span className={`${styles.statusItem} ${styles.stAccent}`} role="listitem">VERIFIABLE</span>
        </div>
      </section>

      <section className={styles.railWrap} aria-label="The proof rail">
        <div className={styles.railPanel}>
          <span className={styles.railScan} aria-hidden />
          <p className={styles.railTitle}>THE PROOF RAIL · six stages, one sealed trace</p>
          <ol className={styles.rail} data-testid="proof-rail">
            {RAIL.map((s, i) => (
              <li key={s.label} className={styles.railStep}>
                <span className={`${styles.railChip} ${styles[s.tone]}`}>{s.n}</span>
                <span className={`${styles.railLabel} ${s.tone === 'anchor' ? styles.railLabelAccent : ''}`}>{s.label}</span>
                {i < RAIL.length - 1 ? <span className={styles.railArrow} aria-hidden>→</span> : null}
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className={styles.stages} data-testid="hiw-stages">
        {STAGES.map((s) => (
          <article key={s.eyebrow} className={styles.stage}>
            <div className={styles.stageCopy}>
              <p className={`${styles.stageEyebrow} ${styles[s.tone]}`}>{s.eyebrow}</p>
              <h3 className={styles.h3}>{s.title}</h3>
              <p className={styles.stageBody}>{s.body}</p>
            </div>
            <div className={styles.stageArtifact}>{s.artifact}</div>
          </article>
        ))}
      </section>

      <section className={styles.closing}>
        <h2 className={styles.closeH2}>Don&apos;t trust the leaderboard. Verify the run.</h2>
        <div className={styles.closeCtas}>
          <Link href="/arena" className={styles.ctaPrimary}>Open the Arena →</Link>
          <Link href="/proof/run_esp_ned_01" className={styles.ctaSecondary}>View a proof record</Link>
        </div>
      </section>
    </main>
  );
}
