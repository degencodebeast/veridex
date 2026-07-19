'use client';
import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { InfoTip } from '@/components/ui/InfoTip';
import { GLOSSARY } from '@/lib/glossary';
import { getMakerLiveAb } from '@/lib/api';
import type { GuardAblationArm, GuardAblationDecision, GuardAblationLeg, GuardAblationView } from '@/lib/contracts';
import styles from './GuardAblationScreen.module.css';

// QuoteGuard Behavior Ablation (F-8 · maker_live_ab.v1) — a deep-link surface reached from the Maker
// Proof Card. It compares the SAME strategy on the SAME recorded tape with QuoteGuard OFF vs ON: does
// the guard change the decision? It is emphatically NOT a leaderboard — it shows/implies NO rank,
// winner, CLV, PnL, ROI, edge, or toxicity ordering, never that Guard ON is automatically better, and
// never that the replay is live. 404 (no recorded ablation for this instance) and transport errors are
// REAL states rendered honestly with no fabricated values. The interpretation note is always present.

type State =
  | { kind: 'loading' }
  | { kind: 'unavailable' } // backend 404 → honest "no recorded ablation for this instance"
  | { kind: 'error' } // transport error → retry, never a fabricated ablation
  | { kind: 'ready'; view: GuardAblationView };

export function GuardAblationScreen({
  instanceId,
  backHref,
  loadAblation = getMakerLiveAb,
}: {
  instanceId: string;
  backHref: string;
  loadAblation?: (instanceId: string) => Promise<GuardAblationView | null>;
}) {
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0); // bumping re-runs the load (RETRY)

  useEffect(() => {
    let active = true;
    setState({ kind: 'loading' });
    loadAblation(instanceId)
      .then((view) => {
        if (!active) return;
        setState(view ? { kind: 'ready', view } : { kind: 'unavailable' });
      })
      .catch(() => { if (active) setState({ kind: 'error' }); });
    return () => { active = false; };
  }, [loadAblation, instanceId, attempt]);

  return (
    <article className={styles.screen} aria-label="QuoteGuard Behavior Ablation">
      <header className={styles.header}>
        <Link href={backHref} className={styles.back}>← Maker Proof Card</Link>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>QuoteGuard Behavior Ablation</h1>
          <Badge variant="behavior-ablation">BEHAVIOR ABLATION</Badge>
          <Badge variant="not-a-leaderboard">NOT A LEADERBOARD</Badge>
          <InfoTip label={GLOSSARY.quoteguard_ablation.label}>{GLOSSARY.quoteguard_ablation.definition}</InfoTip>
        </div>
        <div className={`${styles.subMeta} mono`}>
          maker_live_ab.v1 · same strategy &amp; tape, QuoteGuard OFF vs ON · demonstrates behavior change, not profit or rank
        </div>
      </header>

      {state.kind === 'loading' && <LoadingBody />}
      {state.kind === 'unavailable' && <UnavailableBody instanceId={instanceId} />}
      {state.kind === 'error' && <ErrorBody onRetry={() => setAttempt((n) => n + 1)} />}
      {state.kind === 'ready' && <ReadyBody view={state.view} />}
    </article>
  );
}

// ── loading: skeletons only, never fabricated values ────────────────────────────────────────────
function LoadingBody() {
  return (
    <div className={styles.loading} data-testid="ablation-loading" aria-busy="true">
      <div className={styles.armGrid}>
        <div className={styles.skel}>LOADING GUARD OFF…</div>
        <div className={styles.skel}>LOADING GUARD ON…</div>
      </div>
      <div className={`${styles.skel} ${styles.skelWide}`}>SYNCING TIMELINE…</div>
    </div>
  );
}

// ── unavailable (backend 404): an honest empty state, no values ─────────────────────────────────
function UnavailableBody({ instanceId }: { instanceId: string }) {
  return (
    <div className={styles.stateCard} data-testid="ablation-unavailable">
      <span className={styles.stateGlyph}>○</span>
      <div className={styles.stateTitle}>No recorded ablation is available for this instance</div>
      <p className={styles.stateNote}>
        The maker_live_ab endpoint returned 404 for <span className="mono">{instanceId}</span>. Ablations are generated per instance; this one has none yet.
      </p>
    </div>
  );
}

// ── error: retry, and NO values shown until the request succeeds ────────────────────────────────
function ErrorBody({ onRetry }: { onRetry: () => void }) {
  return (
    <div className={`${styles.stateCard} ${styles.stateCardError}`} data-testid="ablation-error">
      <span className={`${styles.stateGlyph} ${styles.stateGlyphError}`}>!</span>
      <div className={styles.stateTitle}>Couldn&apos;t load the ablation</div>
      <p className={styles.stateNote}>Network error reaching maker_live_ab.v1. No values are shown until the request succeeds.</p>
      <button type="button" className={styles.retry} data-testid="ablation-retry" onClick={onRetry}>RETRY</button>
    </div>
  );
}

// ── ready: identity strip + arms + divergence + (timeline | converged) + interpretation note ────
function ReadyBody({ view }: { view: GuardAblationView }) {
  return (
    <>
      <IdentityStrip view={view} />
      <div className={styles.armGrid}>
        <ArmCard arm={view.guard_off} side="off" testid="ablation-arm-off" />
        <ArmCard arm={view.guard_on} side="on" testid="ablation-arm-on" />
      </div>
      {view.diverges
        ? <DivergentBody view={view} />
        : <ConvergedBody />}
      <InterpretationNote />
    </>
  );
}

function IdentityStrip({ view }: { view: GuardAblationView }) {
  return (
    <div className={styles.identity} data-testid="ablation-identity">
      <div className={styles.idCell}>
        <span className={styles.idLabel}>INSTANCE</span>
        <span className={`${styles.idValue} mono`}>{view.instance_id}</span>
      </div>
      <div className={styles.idCell}>
        <span className={styles.idLabel}>MODE</span>
        <span className={`${styles.idValue} mono`}>{view.mode}</span>
      </div>
      <div className={styles.idCell}><Badge variant="recorded-replay">RECORDED TxLINE REPLAY</Badge></div>
      <div className={styles.idCell}><Badge variant="same-strategy-tape">SAME STRATEGY / SAME TAPE</Badge></div>
      {/* The ablation envelope carries no anchor; the honest render is the literal not_anchored — an
          absent anchor is never implied present (global honesty rule 4). */}
      <div className={styles.idCell}>
        <span className={styles.idLabel}>ANCHOR</span>
        <span className={`${styles.idAnchor} mono`} data-testid="ablation-anchor">not_anchored</span>
      </div>
    </div>
  );
}

function ArmCard({ arm, side, testid }: { arm: GuardAblationArm; side: 'off' | 'on'; testid: string }) {
  return (
    <div className={styles.arm} data-testid={testid}>
      <div className={styles.armHead}>
        <span className={styles.armTitle}>
          QuoteGuard <span className={side === 'on' ? styles.armOn : styles.armOff}>{side.toUpperCase()}</span>
        </span>
        <span className={`${styles.armFlag} ${side === 'on' ? styles.armFlagOn : ''} mono`}>
          guard_enabled: {String(arm.guard_enabled)}
        </span>
      </div>
      <dl className={styles.armBody}>
        <div className={styles.armRow}>
          <dt className="mono">terminal_reason <InfoTip label={GLOSSARY.terminal_reason.label}>{GLOSSARY.terminal_reason.definition}</InfoTip></dt>
          <dd className="mono">{arm.terminal_reason ?? '—'}</dd>
        </div>
        <div className={styles.armRow}>
          <dt className="mono">observations_consumed</dt>
          <dd className="mono">{arm.observations_consumed.toLocaleString()}</dd>
        </div>
        <div className={styles.armRow}>
          <dt className="mono">decisions</dt>
          <dd className="mono">{arm.decisions.length.toLocaleString()}</dd>
        </div>
      </dl>
    </div>
  );
}

function ConvergedBody() {
  return (
    <div className={styles.converged} data-testid="ablation-converged">
      <span className={styles.convergedGlyph}>=</span>
      <Badge variant="diverges-false">DIVERGES: false</Badge>
      <div className={styles.stateTitle}>No behavioral divergence on this replay</div>
      <p className={styles.stateNote}>
        QuoteGuard OFF and ON produced identical decisions across every frame on this tape. divergent_frame_indices is empty — the guard never changed a decision here.
      </p>
    </div>
  );
}

interface TimelineRow {
  index: number;
  off: GuardAblationDecision | null;
  on: GuardAblationDecision | null;
  divergent: boolean;
}

function DivergentBody({ view }: { view: GuardAblationView }) {
  const [openFrame, setOpenFrame] = useState<number | null>(view.divergent_frame_indices[0] ?? null);
  const divergentSet = useMemo(() => new Set(view.divergent_frame_indices), [view.divergent_frame_indices]);

  // Synchronized timeline keyed by frame index: the UNION of both arms' decision indices, so a frame
  // present in only one arm still surfaces (never silently dropped). Sorted ascending by frame.
  const rows = useMemo<TimelineRow[]>(() => {
    const offByIndex = new Map(view.guard_off.decisions.map((d) => [d.index, d]));
    const onByIndex = new Map(view.guard_on.decisions.map((d) => [d.index, d]));
    const indices = Array.from(new Set([...offByIndex.keys(), ...onByIndex.keys()])).sort((a, b) => a - b);
    return indices.map((index) => ({
      index,
      off: offByIndex.get(index) ?? null,
      on: onByIndex.get(index) ?? null,
      divergent: divergentSet.has(index),
    }));
  }, [view, divergentSet]);

  const openRow = openFrame != null ? rows.find((r) => r.index === openFrame) ?? null : null;

  return (
    <>
      <div className={styles.diverges} data-testid="ablation-diverges">
        <Badge variant="diverges-true">DIVERGES: true</Badge>
        <span className={styles.divergesText}>
          Behavior diverges at <span className={styles.divergesCount}>{view.divergent_frame_indices.length} frames</span> —
          divergent_frame_indices <span className={`${styles.divergesList} mono`}>[{view.divergent_frame_indices.join(', ')}]</span>.
          {' '}Same observation feed; the guard changed what the strategy did.
          <InfoTip label={GLOSSARY.divergent_frame.label}>{GLOSSARY.divergent_frame.definition}</InfoTip>
        </span>
      </div>

      <div className={styles.timeline} data-testid="ablation-timeline">
        <div className={styles.timelineHead}>
          <span className={styles.timelineTitle}>SYNCHRONIZED DECISION TIMELINE</span>
          <span className={`${styles.timelineHint} mono`}>keyed by frame index · ◇ = divergent</span>
        </div>
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th className={styles.thFrame}>FRAME</th>
                <th className={styles.thOff}>OFF · KIND</th>
                <th className={styles.thOff}>OFF · REASON CODES</th>
                <th className={styles.thOn}>ON · KIND</th>
                <th className={styles.thOn}>ON · REASON CODES</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.index}
                  className={`${row.divergent ? styles.rowDivergent : ''} ${openFrame === row.index ? styles.rowOpen : ''}`}
                  data-testid={`ablation-frame-${row.index}`}
                  data-divergent={row.divergent || undefined}
                  onClick={() => setOpenFrame((cur) => (cur === row.index ? null : row.index))}
                >
                  <td className={styles.tdFrame}>{row.divergent ? '◇ ' : ''}{row.index}</td>
                  <td className={styles.tdKind}>{row.off?.kind ?? '·'}</td>
                  <td className={styles.tdReason}>{row.off ? row.off.reason_codes.join(', ') || '—' : '·'}</td>
                  <td className={`${styles.tdKind} ${row.divergent ? styles.tdKindOn : ''}`}>{row.on?.kind ?? '·'}</td>
                  <td className={`${styles.tdReason} ${row.divergent ? styles.tdReasonOn : ''}`}>{row.on ? row.on.reason_codes.join(', ') || '—' : '·'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {openRow && <LegDetail row={openRow} />}
      </div>
    </>
  );
}

// Expandable per-frame leg detail (kind · role · price · post_only) for the selected frame, one panel
// per arm. Missing/empty legs read as an honest "no legs" message, never a fabricated quote.
function LegDetail({ row }: { row: TimelineRow }) {
  return (
    <div className={styles.legDetail} data-testid={`ablation-legs-${row.index}`}>
      <div className={styles.legHead}>▾ FRAME {row.index} · LEG DETAIL</div>
      <div className={styles.legGrid}>
        <LegColumn title="OFF" decision={row.off} />
        <LegColumn title="ON" decision={row.on} />
      </div>
    </div>
  );
}

function LegColumn({ title, decision }: { title: string; decision: GuardAblationDecision | null }) {
  const legs = decision?.legs ?? [];
  const suppressed = decision != null && decision.kind !== 'QUOTE' && legs.length === 0;
  return (
    <div className={styles.legCol}>
      <div className={styles.legColHead}>{title} · legs ({legs.length})</div>
      {legs.length > 0 ? (
        legs.map((leg, i) => <LegRow key={i} leg={leg} />)
      ) : (
        <div className={styles.legEmpty}>
          {suppressed
            ? <>no legs — quote suppressed<br /><span className={styles.legReason}>{decision?.reason_codes.join(', ') || 'suppressed'}</span></>
            : 'no legs on this frame'}
        </div>
      )}
    </div>
  );
}

function LegRow({ leg }: { leg: GuardAblationLeg }) {
  return (
    <div className={styles.legRow}>
      <span className="mono">
        {leg.kind} · {leg.role}{leg.post_only ? ' · post_only' : ''}
        {leg.post_only && <InfoTip label={GLOSSARY.post_only_leg.label}>{GLOSSARY.post_only_leg.definition}</InfoTip>}
      </span>
      <span className={`${styles.legPrice} mono`}>{leg.price != null ? leg.price : '—'}</span>
    </div>
  );
}

// Always present (both populated states) — the fixed doctrine note. It never establishes profit or
// rank; the sealed historical Maker benchmark (with its ranking) is a separate surface.
function InterpretationNote() {
  return (
    <div className={styles.note} data-testid="ablation-note">
      <span className={styles.noteGlyph} aria-hidden>ⓘ</span>
      <p>
        This panel demonstrates <strong>how QuoteGuard changes behavior on the same recorded evidence</strong>. It does <strong>not</strong> establish profitability, edge, or rank an agent — there is no CLV, toxicity ordering, PnL, or winner here. The sealed historical Maker benchmark (with its ranking) is a separate surface.
      </p>
    </div>
  );
}
