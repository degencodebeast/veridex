'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { InfoTip } from '@/components/ui/InfoTip';
import { JsonPanel } from '@/components/ui/JsonPanel';
import { proofHref } from '@/lib/deeplinks';
import { fmtBps, fmtPct } from '@/lib/format';
import { QUANTITIES, STABLE_PRICE_CAPTION } from '@/lib/doctrine';
import { GLOSSARY } from '@/lib/glossary';
import { hasRealVenueQuote } from '@/lib/edge-gate';
import type { InspectorRecord } from '@/lib/contracts';
import styles from './InspectorScreen.module.css';

const QLABEL = Object.fromEntries(QUANTITIES.map((q) => [q.id, q.label])) as Record<string, string>;

export function InspectorScreen({ record }: { record: InspectorRecord }) {
  const { clv_explanation: clv } = record;
  // Honest-absence: the doctrine quantities are null when not in the proof artifact —
  // render "—", never a plausible 0.0%/0.000 (no-overclaim). CLV is the real score.
  const fairValueText = clv.fair_value_pct == null || clv.closing_fair_value_pct == null
    ? '—'
    : `${fmtPct(String(clv.fair_value_pct))} → ${fmtPct(String(clv.closing_fair_value_pct))}`;
  // DISPLAY GATE (REQ-2D-501 / AC-2D-501): the venue price, the mispricing gap, and the executable
  // edge derive from the venue quote — they render ONLY when a REAL venue quote backs them. A
  // Fake/paper quote (FakeVenueAdapter's fixed 2.05) has real_venue_quote=false and reads "—".
  const realQuote = hasRealVenueQuote(clv);
  // Mispricing gap is a PROBABILITY-space dislocation, distinct from the EV-space executable edge.
  const mispricingGapText = realQuote && clv.mispricing_gap_bps != null ? fmtBps(clv.mispricing_gap_bps) : '—';
  const execEdgeText = realQuote
    ? `${fmtBps(clv.executable_edge_bps as number)} @ ${(clv.venue_decimal_price as number).toFixed(3)}`
    : '—';
  // Kelly/stake sizing is UNSERVED in both mock and live (SEC-005) — never surfaced regardless of value.
  const stakeText = '—';
  return (
    <article className={styles.inspector}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Decision Inspector</h1>
          <span className={`${styles.meta} mono`}>{record.agent_id} · action #{record.action_seq}</span>
          <Badge variant={record.proof_mode} />
          {record.is_live ? <span className={`${styles.readonly} mono`}>READ-ONLY DURING RUN</span> : null}
        </div>
        <Link href={proofHref(record.run_id)} className={styles.proofLink}>View Full Proof Card →</Link>
      </header>

      <ol className={styles.story}>
        <li className={`${styles.step} ${styles.proposed}`}>
          <span className={styles.stepNo}>1</span><span className={styles.stepLabel}>LLM proposed</span>
        </li>
        <li className={`${styles.step} ${styles.recomputed}`}>
          <span className={styles.stepNo}>2</span><span className={styles.stepLabel}>Law recomputed</span>
        </li>
        <li className={`${styles.step} ${styles.scored}`}>
          <span className={styles.stepNo}>3</span><span className={styles.stepLabel}>Score from evidence</span>
        </li>
      </ol>

      <div className={styles.panels}>
        <JsonPanel title="MarketState" data={record.market_state} />
        <div className={styles.actionPanel}>
          <JsonPanel title="AgentAction" data={record.agent_action} />
          {/* The action params include untrusted LLM claims (reason/confidence/
              claimed_edge_bps) — recorded, never scored (SEC-007). Marked so the
              claim never reads as authoritative on this trust screen. */}
          <p className={styles.actionNote}>
            ⚠ params include untrusted LLM claims (reason · confidence · claimed_edge_bps) — recorded, not scored
          </p>
        </div>
        <JsonPanel title="Deterministic Recompute" data={record.recompute} accent />
        <section className={styles.clv} aria-label="CLV explanation">
          <div className={styles.clvTitle}>CLV Explanation</div>
          <dl className={styles.quantities}>
            <div className={styles.qrow}><dt className={styles.qlabel}>{QLABEL.fair_value} <InfoTip label={GLOSSARY.fair_value.label}>{GLOSSARY.fair_value.definition}</InfoTip></dt><dd className={`${styles.qval} mono`}>{fairValueText}</dd></div>
            {/* Mispricing Gap — prob-space dislocation; glossary-sourced label, DISTINCT from Executable Edge (never labeled "edge"). */}
            <div className={styles.qrow}><dt className={styles.qlabel}>{GLOSSARY.mispricing_gap.label} <InfoTip label={GLOSSARY.mispricing_gap.label}>{GLOSSARY.mispricing_gap.definition}</InfoTip></dt><dd className={`${styles.qval} mono`}>{mispricingGapText}</dd></div>
            <div className={styles.qrow}><dt className={styles.qlabel}>{QLABEL.executable_edge} <InfoTip label={GLOSSARY.executable_edge.label}>{GLOSSARY.executable_edge.definition}</InfoTip></dt><dd className={`${styles.qval} mono`}>{execEdgeText}</dd></div>
            <div className={styles.qrow}><dt className={styles.qlabel}>{QLABEL.clv} <InfoTip label={GLOSSARY.clv.label}>{GLOSSARY.clv.definition}</InfoTip></dt><dd className={`${styles.qval} mono`}>{fmtBps(clv.clv_bps)}{clv.clv_low_sample ? <span className={`${styles.lowSample} mono`}> · low sample</span> : null}</dd></div>
            <div className={styles.qrow}><dt className={styles.qlabel}>{QLABEL.stake} <InfoTip label={GLOSSARY.kelly.label}>{GLOSSARY.kelly.definition}</InfoTip></dt><dd className={`${styles.qval} mono`}>{stakeText}</dd></div>
          </dl>
          <p className={styles.clvPlain}>{clv.plain}</p>
          <span className={`${styles.scoreChip} mono`}>SCORE = {fmtBps(clv.clv_bps)}</span>
          <p className={styles.stableNote}>{STABLE_PRICE_CAPTION}</p>
        </section>
      </div>

      {record.untrusted_llm ? (
        <section className={styles.untrusted} aria-label="Untrusted LLM metadata">
          <div className={styles.untrustedHead}>⚠ UNTRUSTED LLM METADATA · NOT AN INPUT TO SCORE</div>
          <JsonPanel data={{ model: record.untrusted_llm.model, confidence: record.untrusted_llm.confidence, claimed_edge_bps: record.untrusted_llm.claimed_edge_bps }} />
          <p className={styles.rationale}>{record.untrusted_llm.rationale}</p>
        </section>
      ) : null}
    </article>
  );
}
