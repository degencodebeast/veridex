'use client';
import Link from 'next/link';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { availableModes, resolveMode, type StudioMode } from '@/lib/studio/coupling';
import { ARCHETYPES, SPORTS_ACTION_TYPES, type Archetype, type ExecutionMode, type SourceMode } from '@/lib/catalog';
import { STRATEGY_TEMPLATES, COMPLEXITY_LABEL, type StrategyTemplate } from '@/lib/studio/templates';
import { buildPreflightPreview, PREFLIGHT_DISCLAIMER } from '@/lib/studio/preflight';
import { DEFAULT_POLICY_ENVELOPE, MM_POLICY_ENVELOPE } from '@/lib/config/policy';
import { GLOSSARY } from '@/lib/glossary';
import { deployAgent, DeployPreflightError, type DeployAgentPayload, type DeployAgentResult } from '@/lib/api';
import styles from './StudioScreen.module.css';

// ONE stable Idempotency-Key per submit (I-3 header contract). Reused verbatim across a
// retry/timeout so a retried deploy reconciles to the SAME instance (never a fresh key, which the
// backend would treat as a second logical deploy). `crypto.randomUUID` is present in the browser and
// the jsdom/Node test env; the fallback keeps this pure/total in any exotic runtime.
function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `idem_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

// The QuoteGuard/MM template id — the deploy discriminator for the MM family (decision 2). Driving
// the MM payload off the SELECTED TEMPLATE (not the archetype) is deliberate: quoteguard_mm reuses
// the `baseline` archetype, so mapping by archetype alone would hijack every manual baseline pick.
const MM_TEMPLATE_ID = 'quoteguard_mm';

// F-1: the det-Drift template id — the deploy discriminator for the real `cumulative-drift` detector.
// Like MM, the STRATEGY is driven off the selected TEMPLATE (not the archetype): det-Drift reuses the
// `momentum` archetype for mode coupling, so a manual momentum pick still deploys momentum-sharp while
// THIS template deploys cumulative-drift. (build_agent accepts `cumulative-drift`; config.py:183.)
const DET_DRIFT_TEMPLATE_ID = 'det_drift';

// The MM tape catalog KEY (NOT a path/fixture) resolved server-side via the mm_tape_resolver seam.
// This is the REAL-DATA maker replay tape registered in the production catalog
// (veridex.mm_strategy.session_factory): REAL Polymarket 10-level order-book depth + REAL TxLINE 1X2
// fair value for FIFA World Cup France v Morocco (fixture 18209181), replayed dry-run. It is
// research-grade recorded data (v1 pack, NOT R3-sealed / cryptographically-genuine) — the label
// stays honest to that (see the card/preflight copy below). Wiring the demo to this key is what
// makes the real Studio click path RESOLVE (the parked `synthetic-mm-mechanism-v1` key was never
// registered, so the actual UI deploy fail-closed with MMTapeNotFoundError).
const MM_TAPE_REF = 'pmxt-txline-mm-18209181-v1';
const MM_FIXTURE_ID = 18209181;

// Honest judge-facing provenance for the deployable QuoteGuard/MM card. This is a SIMULATED REPLAY
// (dry-run, no live money) of REAL recorded in-play data — Polymarket 10-level depth + TxLINE 1X2
// fair value, FIFA World Cup France v Morocco — research-grade (v1 pack, NOT R3-sealed / genuine).
// The blurb OVERRIDES the template's own stale "synthetic canned fixture" text at the render layer
// (the template catalog is not wired to the real tape); event/team branding is now CORRECT because
// this is genuinely recorded France v Morocco data, not a canned/synthetic fixture.
const MM_CARD_PROVENANCE =
  'Polymarket depth + TxLINE FV · France v Morocco · simulated replay (real recorded data, research-grade) · dry-run · live-money disabled';
const MM_CARD_BLURB =
  'Market-making / quote-guard rules with inventory + two-sided quoting. Runs the quoteguard-mm family as a SIMULATED REPLAY of REAL recorded in-play data — Polymarket 10-level order-book depth + TxLINE 1X2 fair value, FIFA World Cup France v Morocco (fixture 18209181) — dry-run only (no live money); research-grade v1 pack (not R3-sealed).';

// Clamp any execution mode to what the MM family permits: the backend fail-closes MM on
// execution_mode == 'live_guarded', so the MM card only ever emits paper | dry_run.
function mmExecutionMode(exec: ExecutionMode): 'paper' | 'dry_run' {
  return exec === 'dry_run' ? 'dry_run' : 'paper';
}

// F-1: the honest, EXHAUSTIVE strategy resolution. The old `toStrategy` ended in a silent
// `return 'momentum-sharp'`, so value_clv / contrarian / stale_line in a deterministic mode all deployed
// an IDENTICAL momentum-sharp agent under three different card names — that is the dishonesty F-1 removes.
// This resolver is bounded by what the deploy path actually supports (build_agent's Literal
// baseline|momentum|momentum-sharp|cumulative-drift|llm, plus the separate quoteguard-mm MM seam) and has
// NO default fallthrough: an unmapped combo returns a typed `unsupported` verdict so the UI can disable
// deploy — a strategy the card does not name is NEVER emitted.
export type StrategyResolution =
  | { supported: true; strategy: string }
  | { supported: false; reason: string };

export function resolveStrategy(
  templateId: string | null, archetype: Archetype, mode: StudioMode,
): StrategyResolution {
  // Template-driven overrides — the deploy discriminator is the SELECTED template, not the archetype
  // (so a manual archetype pick never hijacks a template family; mirrors the MM carve-out).
  if (templateId === MM_TEMPLATE_ID) return { supported: true, strategy: 'quoteguard-mm' };
  if (templateId === DET_DRIFT_TEMPLATE_ID) return { supported: true, strategy: 'cumulative-drift' };
  // Directional path — an honest map onto the build_agent Literal:
  //   • any llm-capable archetype in LLM mode → the generic `llm` agent (value_clv/baseline are
  //     LLM-locked in coupling.ts, so this branch only ever sees momentum/contrarian/stale_line).
  //   • momentum (deterministic) → momentum-sharp; baseline (deterministic) → baseline.
  if (mode === 'llm') return { supported: true, strategy: 'llm' };
  if (archetype === 'momentum') return { supported: true, strategy: 'momentum-sharp' };
  if (archetype === 'baseline') return { supported: true, strategy: 'baseline' };
  // value_clv / contrarian / stale_line in a deterministic mode: NO distinct backend strategy exists.
  // Return unsupported (never a silent momentum-sharp) — the deploy affordance is disabled for these.
  return {
    supported: false,
    reason: `${archetype} has no deterministic backend strategy — switch to LLM mode (if available), or pick a supported template (Momentum, det-Drift, or QuoteGuard/MM)`,
  };
}

// Thrown if a deploy payload is somehow built for an unsupported combo (defense-in-depth: the UI
// disables the button first). Making this a hard throw means a strategy the card does not name can
// never be silently substituted, even if a caller bypasses the disabled affordance.
export class UnsupportedStrategyError extends Error {
  constructor(public readonly archetype: Archetype, public readonly mode: StudioMode, reason: string) {
    super(`unsupported strategy for archetype=${archetype} mode=${mode}: ${reason}`);
    this.name = 'UnsupportedStrategyError';
  }
}

// Build the non-secret deploy payload from the pinned Studio config + policy envelope.
// source_mode is the OPERATOR's choice (never hardcoded 'live'): the demo defaults to a working
// REPLAY deploy (recorded pack, proof-only). A 'live' deploy stays fail-closed on the backend
// (named feed_health 422) until a live feed is wired — the honest live path is T20b/operator.
//
// fu-ii5: when the SELECTED TEMPLATE is quoteguard_mm, emit the frozen `quoteguard-mm` MM family
// (strategy discriminator + an `mm` MakerDeployConfig subset), pinned to replay + paper/dry_run so
// the backend fail-closed MM preflight (_check_mm) accepts it. Every other selection keeps the
// existing directional behavior UNCHANGED (a manual `baseline` archetype still → directional).
export function buildDeployPayload(
  archetype: Archetype, mode: StudioMode, exec: ExecutionMode, source: SourceMode,
  templateId: string | null,
): DeployAgentPayload {
  if (templateId === MM_TEMPLATE_ID) {
    return {
      template_id: MM_TEMPLATE_ID,
      agent_id: `studio-${MM_TEMPLATE_ID}`,
      strategy: 'quoteguard-mm',
      source_mode: 'replay', // MM allowed ONLY in replay (backend rejects source_mode=='live')
      execution_mode: mmExecutionMode(exec), // paper | dry_run only (no live_guarded)
      // The PMXT-coherent MM envelope (poly / the real home-win token) — NOT the directional
      // DEFAULT_POLICY_ENVELOPE (sxbet / 1X2). The backend pins manifest.market = market_allowlist[0]
      // and the tape's book quotes on that token, so this identity is REQUIRED for an ATTEMPTED leg.
      market_allowlist: MM_POLICY_ENVELOPE.market_allowlist, // non-empty (empty → MM reject)
      venue_allowlist: MM_POLICY_ENVELOPE.venue_allowlist,
      min_edge_bps: MM_POLICY_ENVELOPE.min_edge_bps,
      max_stake: MM_POLICY_ENVELOPE.max_stake,
      window_id: `studio-${MM_TEMPLATE_ID}`,
      fixture_id: MM_FIXTURE_ID, // the REAL Polymarket/TxLINE fixture (France v Morocco)
      end_rule: 'pre_match',
      mm: {
        tape_ref: MM_TAPE_REF,
        guard_enabled: true,
        tif: 'GTC',
        max_orders_per_run: 3,
        max_orders_per_session: 10,
        max_orders_per_day: 20,
        max_session_loss: 0,
        max_daily_loss: 0,
      },
    };
  }
  // F-1: det-Drift — the real `cumulative-drift` detector as a standalone Studio deploy. Same directional
  // payload/envelope as the archetype path (the backend applies its own cum_drift_* knob defaults —
  // config.py:183), with the strategy fixed by the template id. NO invented drift params on the wire.
  if (templateId === DET_DRIFT_TEMPLATE_ID) {
    return {
      template_id: DET_DRIFT_TEMPLATE_ID,
      agent_id: `studio-${DET_DRIFT_TEMPLATE_ID}`,
      strategy: 'cumulative-drift',
      source_mode: source,
      execution_mode: exec,
      market_allowlist: DEFAULT_POLICY_ENVELOPE.market_allowlist,
      venue_allowlist: DEFAULT_POLICY_ENVELOPE.venue_allowlist,
      min_edge_bps: DEFAULT_POLICY_ENVELOPE.min_edge_bps,
      max_stake: DEFAULT_POLICY_ENVELOPE.max_stake,
      window_id: `studio-${DET_DRIFT_TEMPLATE_ID}`,
      fixture_id: 1,
      end_rule: 'pre_match',
    };
  }
  // Directional path — resolve the strategy EXHAUSTIVELY (no silent momentum-sharp fallthrough). An
  // unsupported combo throws rather than emit a strategy the card does not name; the UI disables the
  // deploy button for these, so this throw is defense-in-depth against a bypassed affordance.
  const resolution = resolveStrategy(templateId, archetype, mode);
  if (!resolution.supported) {
    throw new UnsupportedStrategyError(archetype, mode, resolution.reason);
  }
  return {
    template_id: archetype,
    agent_id: `studio-${archetype}`,
    strategy: resolution.strategy,
    source_mode: source,
    execution_mode: exec,
    market_allowlist: DEFAULT_POLICY_ENVELOPE.market_allowlist,
    venue_allowlist: DEFAULT_POLICY_ENVELOPE.venue_allowlist,
    min_edge_bps: DEFAULT_POLICY_ENVELOPE.min_edge_bps,
    max_stake: DEFAULT_POLICY_ENVELOPE.max_stake,
    window_id: `studio-${archetype}`,
    fixture_id: 1,
    end_rule: 'pre_match',
  };
}

// Honesty (law_hash / Create-wizard ruling): the config pin is an affordance, NOT a fabricated
// proof-flavored digest. The config is frozen at entry; real evidence/score/manifest hashes only
// appear on the Proof Card AFTER a sealed run. So there is no client-side "config hash" here.

function Section({ n, title, inactive, children }: { n: string; title: string; inactive?: boolean; children: ReactNode }) {
  return (
    <section
      className={`${styles.section} ${inactive ? styles.inactive : ''}`}
      data-testid={`section-${n}`}
      data-inactive={inactive ? 'true' : 'false'}
    >
      <h2 className={styles.h2}><span className={styles.num}>{n}</span> {title}</h2>
      {inactive ? <p className={styles.stub}>Not applicable in this mode.</p> : children}
    </section>
  );
}

export function StudioScreen({
  onPin = () => {},
  running = false,
  deployGate = (deployButton) => deployButton,
}: {
  onPin?: (result: DeployAgentResult) => void;
  running?: boolean;
  // Render seam for the owner-scoped DEPLOY affordance (auth-contract@1). The page injects the auth
  // gate here (wrap the button with <AuthGate>, or replace it with a fail-closed prompt when Privy is
  // unconfigured) so an unauthenticated operator can still configure a draft but cannot deploy. Default
  // is identity — the authenticated path — so component tests exercise the deploy button directly.
  deployGate?: (deployButton: ReactNode) => ReactNode;
}) {
  // F-1: default to `baseline` — a genuinely deployable strategy. value_clv (the prior default) is
  // LLM-locked AND has no distinct deterministic backend strategy, so under the honest exhaustive
  // mapping it is never deployable; defaulting the primary CTA to a non-deployable archetype would be a
  // broken-looking (and dishonest-feeling) first-run. baseline is also LLM-locked, so the AC-007
  // LLM-lock behavior is preserved. value_clv remains selectable (its deploy is honestly disabled).
  const [archetype, setArchetype] = useState<Archetype>('baseline');
  const [mode, setMode] = useState<StudioMode>('numeric');
  const [exec, setExec] = useState<ExecutionMode>('paper');
  // fu-ii5: the SELECTED template id — the deploy discriminator for the MM family. null once the
  // operator hand-edits archetype/mode (a manual edit is no longer a template-driven deploy), which
  // keeps a manual `baseline` pick on the directional path instead of hijacking it into MM.
  const [templateId, setTemplateId] = useState<string | null>(null);
  // Demo-safe default: a working REPLAY deploy (recorded pack). 'live' stays fail-closed on the
  // backend until a live feed is wired — never dressed up as live/real-money from the demo.
  const [source, setSource] = useState<SourceMode>('replay');
  // The last-pinned snapshot — edits are diffed against it and applied as a NEW version on pin.
  const [baseline, setBaseline] = useState<{ archetype: Archetype; mode: StudioMode; exec: ExecutionMode }>(
    { archetype: 'baseline', mode: 'numeric', exec: 'paper' },
  );
  // Whether the current draft has been pinned (drives the honest "Config pinned ✓" affordance).
  const [pinned, setPinned] = useState(false);
  // Server-owned deploy state — the run_id the pinned instance launched, or the NAMED preflight
  // failure. There is NO client-fabricated deploy handle: the endpoint is the source of truth.
  const [runId, setRunId] = useState<string | null>(null);
  const [preflightFailure, setPreflightFailure] = useState<string[] | null>(null);
  const [deploying, setDeploying] = useState(false);
  // The current submit's stable Idempotency-Key (I-3), tagged with the config FINGERPRINT it was
  // minted for. Held OUTSIDE render state because it must be read/written synchronously within one
  // submit and survive a retry without re-rendering. The key is REUSED iff the deploy payload is
  // byte-identical to when it was minted — so a retry after a timeout reconciles to the SAME instance,
  // while a genuine config change mints a fresh key (honoring the backend's 409-on-different-config
  // contract). Keying reuse off the actual payload — not off click events — means a NO-OP interaction
  // (re-clicking the already-active segment, or a resolveMode snap-back) never disturbs the key.
  // Cleared on a successful deploy (the next submit is a new logical deploy); KEPT on failure.
  const idempotencyKeyRef = useRef<{ key: string; fingerprint: string } | null>(null);

  const modes = availableModes(archetype);
  // fu-ii5: the MM template is restricted to the fail-closed-safe modes — the source/exec selectors
  // must not offer live / live_guarded for it (an operator cannot pick a mode the backend rejects).
  const isMM = templateId === MM_TEMPLATE_ID;
  // F-1: resolve the honest strategy for the CURRENT selection. When the combo has no distinct backend
  // strategy (value_clv / contrarian / stale_line in a deterministic mode), deploy is disabled and the
  // reason is surfaced — never a silent momentum-sharp substitution.
  const strategyResolution = resolveStrategy(templateId, archetype, mode);
  const deployUnsupported = !strategyResolution.supported;
  const unsupportedReason = strategyResolution.supported ? null : strategyResolution.reason;

  // AC-007 snap-back: whenever archetype changes, re-resolve the current mode. A manual archetype/
  // mode edit clears the selected template — the deploy reverts to the directional path (fu-ii5).
  // Config edits don't touch the Idempotency-Key directly — key reuse is decided at submit time by the
  // config fingerprint (see pin()). That makes a NO-OP interaction (re-clicking the active segment, a
  // resolveMode snap-back) a true no-op for idempotency, while a genuine change still mints a fresh key.
  function onArchetype(next: Archetype) {
    setArchetype(next);
    setMode((m) => resolveMode(next, m));
    setTemplateId(null);
  }
  function onMode(next: StudioMode) {
    setMode(resolveMode(archetype, next));
    setTemplateId(null);
  }
  // Selecting a BUILT template applies its archetype + default mode through the existing coupling
  // (snap-back preserved) and tracks the template id as the deploy discriminator. Locked heavy
  // extensions (Arb/Spread) are not selectable; the deployable MM template is. For the MM template
  // the source/exec are clamped to the fail-closed-safe MM modes (replay + paper/dry_run).
  function applyTemplate(t: StrategyTemplate) {
    setArchetype(t.archetype);
    setMode(resolveMode(t.archetype, t.defaultMode));
    setTemplateId(t.id);
    if (t.id === MM_TEMPLATE_ID) {
      setSource('replay');
      // Default the MM card to Dry Run: replay+paper mints OPS telemetry ONLY, while replay+dry_run
      // is the mode that produces a dry-run receipt with an ATTEMPTED leg — so the headline MM click
      // path is receipt-producing by default (the operator may still switch to Paper). No live_guarded.
      setExec('dry_run');
    }
  }

  // #4 reviewable diff: current draft vs last-pinned baseline (a structured before→after patch).
  const diffEntries = [
    { field: 'archetype', before: baseline.archetype as string, after: archetype as string },
    { field: 'mode', before: baseline.mode as string, after: mode as string },
    { field: 'execution_mode', before: baseline.exec as string, after: exec as string },
  ].filter((e) => e.before !== e.after);

  async function pin() {
    // Pin the config locally (freeze the draft as the new version) — this is the local affordance,
    // DISTINCT from the deploy outcome below. Navigation is NOT triggered here: an HONEST deploy must
    // AWAIT the real result and navigate ONLY on success (never fire-and-forget past a failure).
    setBaseline({ archetype, mode, exec }); // applied as the new pinned version
    setPinned(true);
    // DEPLOY for real: POST the config → fail-closed preflight → pinned instance + async run_id.
    // The run_id / named preflight failure are server-owned (no loose client-only deploy state).
    setDeploying(true);
    setPreflightFailure(null);
    setRunId(null);
    try {
      // Build the payload INSIDE the try: buildDeployPayload throws UnsupportedStrategyError for a
      // bypassed-affordance deploy (a strategy the card does not name). Keeping the build outside the
      // try left that throw as an UNHANDLED rejection AFTER setDeploying(true) — the UI stuck on
      // "DEPLOYING…" forever with no surfaced failure. Inside the try, the throw routes to the same
      // visible fail-closed channel below and `finally` resets the deploying state (F-1 fold).
      const payload = buildDeployPayload(archetype, mode, exec, source, templateId);
      // ONE stable Idempotency-Key per logical submit (I-3). Reuse the held key IFF this payload is
      // byte-identical to the one it was minted for — so a retry after a timeout reconciles to the SAME
      // instance, while a genuine config change mints a fresh key (the backend 409s a reused key with a
      // different config). Fingerprinting the payload (not click events) makes a no-op interaction inert.
      const fingerprint = JSON.stringify(payload);
      if (!idempotencyKeyRef.current || idempotencyKeyRef.current.fingerprint !== fingerprint) {
        idempotencyKeyRef.current = { key: newIdempotencyKey(), fingerprint };
      }
      const idempotencyKey = idempotencyKeyRef.current.key;
      const result = await deployAgent(payload, idempotencyKey);
      // SUCCESS: use the REAL result — surface the run_id AND hand the resolved instance to the
      // navigation callback so the page routes to /instances/{instance_id}. Clear the key: the next
      // submit is a new logical deploy.
      setRunId(result.run_id);
      idempotencyKeyRef.current = null;
      onPin(result); // navigate ON SUCCESS ONLY, with the awaited result — never before it exists.
    } catch (err) {
      // FAILURE: STAY on Studio and surface the named fail-closed check in place (no navigation).
      // KEEP the Idempotency-Key so a retry of this same submit reuses it (idempotent reconcile).
      if (err instanceof DeployPreflightError) setPreflightFailure(err.failedChecks);
      // A bypassed-affordance unsupported combo surfaces as a NAMED fail-closed check — never a
      // silent stuck spinner (the deploy affordance is disabled first; this is defense-in-depth).
      else if (err instanceof UnsupportedStrategyError) setPreflightFailure(['unsupported_strategy']);
      else setPreflightFailure(['deploy_unavailable']);
    } finally {
      setDeploying(false);
    }
  }

  // PREFLIGHT PREVIEW — fully (A) real config (codex option 3): threshold + rule-config + disclaimer.
  // NO computed/estimated pre-run edge value; pre-run edge is a per-run sealed proof quantity.
  // DEFAULT_POLICY_ENVELOPE is a module-level const; memo deps are empty (stable reference).
  const preview = useMemo(() => buildPreflightPreview(DEFAULT_POLICY_ENVELOPE), []);

  return (
    <section className={styles.screen} aria-label="Agent Studio">
      <header className={styles.head}>
        <h1 className={styles.title}>Agent Studio</h1>
        {running ? (
          <span className={`${styles.roMode} mono`} data-testid="ro-mode">MODE · {mode}</span>
        ) : (
          <SegmentedControl<StudioMode>
            ariaLabel="Strategy mode" value={mode} onChange={onMode}
            options={modes.map((m) => ({ value: m.mode, label: m.mode === 'llm' ? 'LLM' : m.mode === 'numeric' ? 'Numeric' : 'Rule', locked: m.locked }))}
          />
        )}
      </header>

      <div className={styles.layout}>
        <div className={styles.sections}>
          <Section n="01" title="Identity & archetype">
            <div className={styles.cards} data-testid="strategy-cards">
              {STRATEGY_TEMPLATES.map((t) => {
                // F-1: LLM-Drift is an ARENA-ONLY contestant — `strategy:"llm-drift"` does NOT exist in
                // the deploy path (it lives ONLY in veridex/runtime/arena_comparison.py). So it is NOT a
                // selectable deploy card: it renders a "Use in Arena" affordance and NO deploy button,
                // and it never sets a deploy template. (Operator reconciliation 2026-07-19.)
                if (t.arenaOnly) {
                  return (
                    <div key={t.id} className={`${styles.card} ${styles.cardArena}`} data-testid="arena-only-card">
                      <span className={styles.cardLabel}>{t.label}</span>
                      <span className={styles.cardComplexity}>arena contestant · no Studio deploy</span>
                      <span className={styles.cardBlurb}>{t.blurb}</span>
                      <Link className={styles.arenaLink} href="/arena" data-testid="use-in-arena">Use in Arena →</Link>
                    </div>
                  );
                }
                // fu-ii5: a heavy extension is LOCKED unless it is per-template `deployable`. Only
                // Arb/Spread stays Phase-3-locked; quoteguard_mm + det-Drift are deployable.
                const locked = t.complexity === 'heavy-extension' && !t.deployable;
                // The MM card carries a SIMULATED-REPLAY provenance label + blurb (real recorded
                // Polymarket/TxLINE data, dry-run, live-money disabled). Keyed off the MM id specifically
                // (NOT `deployable`, which now also covers det-Drift) so det-Drift keeps its own copy.
                const isMMCard = t.id === MM_TEMPLATE_ID;
                const complexityLabel = isMMCard ? MM_CARD_PROVENANCE : COMPLEXITY_LABEL[t.complexity];
                const cardBlurb = isMMCard ? MM_CARD_BLURB : t.blurb;
                return (
                  <button
                    key={t.id}
                    type="button"
                    className={`${styles.card} ${locked ? styles.cardPhase3 : ''}`}
                    disabled={locked || running}
                    aria-disabled={locked || undefined}
                    onClick={() => { if (!locked) applyTemplate(t); }}
                  >
                    <span className={styles.cardLabel}>{t.label}</span>
                    <span className={styles.cardComplexity}>{complexityLabel}</span>
                    <span className={styles.cardBlurb}>{cardBlurb}</span>
                  </button>
                );
              })}
            </div>
            <label className={styles.field}>
              <span className={styles.label}>Archetype</span>
              <select aria-label="Archetype" className={styles.select} value={archetype} disabled={running} onChange={(e) => onArchetype(e.target.value as Archetype)}>
                {ARCHETYPES.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <p className={styles.hint}>
              Source mode{' '}
              <InfoTip label={GLOSSARY.source_mode.label}>{GLOSSARY.source_mode.definition}</InfoTip>: STUDIO (reproducible).
              {' '}Proof mode{' '}
              <InfoTip label={GLOSSARY.proof_mode.label}>{GLOSSARY.proof_mode.definition}</InfoTip>: reproducible.
              {' '}BYOA agents are verified, not reproducible (Phase-3).
            </p>
          </Section>

          <Section n="02" title="Decision Shell (LLM)" inactive={mode !== 'llm'}>
            <div className={styles.fence} data-testid="llm-fence">
              <span className={styles.fenceLabel}>⚠ NOT AN INPUT TO SCORE</span>
              <p className={styles.hint}>The LLM emits a constrained AgentAction; the law recomputes every action. Rationale/confidence are untrusted.</p>
              <div className={styles.actionTypes}>
                {SPORTS_ACTION_TYPES.map((t) => <span key={t} className={styles.actionType}>{t}</span>)}
              </div>
            </div>
          </Section>

          <Section n="03" title="Deterministic config" inactive={mode === 'llm'}>
            <p className={styles.hint}>{mode === 'numeric' ? 'Numeric thresholds: min_edge_bps, conviction, quote-age.' : 'Rule rows: market_key · side · window · max price · min edge · action.'}</p>
            <div className={styles.kv}><span>min_edge_bps</span><span className="mono">{DEFAULT_POLICY_ENVELOPE.min_edge_bps}</span></div>
            <div className={styles.kv}><span>max_quote_age_s</span><span className="mono">{DEFAULT_POLICY_ENVELOPE.max_quote_age_s}</span></div>
          </Section>

          <Section n="04" title="Market scope">
            <div className={styles.chips}>
              {DEFAULT_POLICY_ENVELOPE.market_allowlist.map((m) => <span key={m} className={styles.chip}>{m}</span>)}
            </div>
          </Section>

          <Section n="05" title="Policy envelope & execution">
            <div className={styles.kv}>
              <span>
                max_stake{' '}
                <InfoTip label={GLOSSARY.kelly.label}>{GLOSSARY.kelly.definition}</InfoTip>
              </span>
              <span className="mono">{DEFAULT_POLICY_ENVELOPE.max_stake}</span>
            </div>
            <div className={styles.kv}><span>kill_switch</span><span className="mono">{String(DEFAULT_POLICY_ENVELOPE.kill_switch)}</span></div>
            <label className={styles.field}>
              <span className={styles.label}>
                Source mode{' '}
                <InfoTip label={GLOSSARY.source_mode.label}>{GLOSSARY.source_mode.definition}</InfoTip>
              </span>
              {running ? (
                <span className="mono" data-testid="source-mode-ro">{source}</span>
              ) : (
                <SegmentedControl<SourceMode>
                  ariaLabel="Source mode" value={source} onChange={setSource}
                  options={isMM
                    ? [{ value: 'replay', label: 'Replay' }] // MM: replay only (backend rejects live)
                    : [{ value: 'replay', label: 'Replay' }, { value: 'live', label: 'Live' }]}
                />
              )}
            </label>
            {source === 'live' ? (
              <p className={styles.hint} data-testid="live-fail-closed-note">
                Live deploy stays fail-closed (named <span className="mono">feed_health</span> preflight) until a live
                feed is wired — the demo runs a recorded REPLAY pack, never real-money execution.
              </p>
            ) : null}
            <label className={styles.field}>
              <span className={styles.label}>
                Execution mode{' '}
                <InfoTip label={GLOSSARY.execution_mode.label}>{GLOSSARY.execution_mode.definition}</InfoTip>
              </span>
              {running ? (
                <span className="mono">{exec}</span>
              ) : (
                <SegmentedControl<ExecutionMode>
                  ariaLabel="Execution mode" value={exec} onChange={setExec}
                  options={isMM
                    ? [{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }] // MM: no live_guarded
                    : [{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }, { value: 'live_guarded', label: 'Live Guarded' }]}
                />
              )}
            </label>
          </Section>
        </div>

        <div className={styles.side}>
          <div className={styles.diffPanel} data-testid="config-diff">
            <h2 className={styles.h2}>Reviewable changes</h2>
            <p className={styles.hint}>Edits are a structured patch, applied as a new pinned version on PIN — never a live mutation of a running agent.</p>
            {diffEntries.length === 0 ? (
              <p className={styles.stub}>No pending changes.</p>
            ) : (
              diffEntries.map((e) => (
                <div key={e.field} className={styles.diffRow} data-testid={`diff-${e.field}`}>
                  <span className={styles.diffField}>{e.field}</span>
                  <span className={styles.diffBefore}>{e.before}</span>
                  <span className={styles.diffArrow}>→</span>
                  <span className={styles.diffAfter}>{e.after}</span>
                </div>
              ))
            )}
          </div>

          {/*
           * PREFLIGHT PREVIEW — codex option 3 (decided doctrine, do NOT deviate).
           * Shows the REAL min-edge THRESHOLD ("Minimum executable edge ≥ N bps"),
           * the policy envelope, and the rule-config table. NO computed/estimated
           * pre-run edge value. NO proof styling. NO green check / verifier icon.
           * NO ALLOW/DENY badge computed from a sample edge.
           *
           * Forbidden from this panel: recomputed / verified / proven / law result /
           * executable edge (as a value) / CLV / score / eligible / policy-approved.
           */}
          <aside className={styles.preflight} data-testid="preflight">
            <h2 className={styles.h2}>Preflight Preview</h2>

            {/* Threshold row — the REAL config threshold; NOT a computed edge value. */}
            <div className={styles.threshold} data-testid="threshold-row">
              <span className={styles.thresholdLabel}>
                Minimum executable edge{' '}
                <InfoTip label={GLOSSARY.executable_edge.label}>{GLOSSARY.executable_edge.definition}</InfoTip>
              </span>
              <span className={`mono ${styles.thresholdValue}`}>
                ≥ {preview.min_edge_threshold_bps} bps
              </span>
            </div>

            {/* Selected execution mode (state, not from policy envelope). */}
            <div className={styles.kv}>
              <span>execution_mode</span>
              <span className="mono">{exec}</span>
            </div>

            {/* Rule-config table — all policy envelope fields derived from the real config. */}
            <div className={styles.ruleTable} data-testid="rule-config">
              <div className={styles.ruleHeader}>Rule config</div>
              {preview.rule_config.map((r) => (
                <div key={r.field} className={styles.ruleRow}>
                  <span className={styles.ruleField}>{r.field}</span>
                  <span className={`mono ${styles.ruleValue}`}>{r.value}</span>
                </div>
              ))}
            </div>

            {/* Disclaimer — verbatim, single-sourced from lib/studio/preflight (codex-pinned). */}
            <p className={styles.disclaimer} data-testid="preflight-disclaimer">{PREFLIGHT_DISCLAIMER}</p>

            {/* HONEST provenance for the MM family: the queued run is a SIMULATED REPLAY (dry-run, no
                live money) of REAL recorded in-play data — Polymarket depth + TxLINE fair value, France
                v Morocco — research-grade recorded data, NOT a live feed and NOT R3-sealed/genuine. */}
            {isMM ? (
              <p className={styles.hint} data-testid="mm-provenance-note">
                QuoteGuard/MM replays REAL recorded in-play data — Polymarket 10-level depth + TxLINE 1X2
                fair value, France v Morocco (fixture 18209181) — as a simulated dry-run replay: no live
                TxLINE feed, no live-money execution; research-grade recorded data (not R3-sealed).
              </p>
            ) : null}

            {running ? (
              <p className={styles.roBanner} data-testid="run-readonly">Config is read-only during a scored run (SEC-006). Edits create a new version, never a live mutation.</p>
            ) : (
              // The deploy affordance is auth-gated by the page via deployGate: an unauthenticated
              // operator sees the login gate (or a fail-closed prompt) in place of this button, so a
              // bearer-less owner-scoped POST is structurally impossible (the button is absent, not
              // merely disabled). Default deployGate is identity (authenticated path).
              deployGate(
                <button type="button" className={styles.pin} onClick={pin} disabled={deploying || deployUnsupported}>
                  {deploying ? 'DEPLOYING…' : 'PIN CONFIG & QUEUE RUN →'}
                </button>,
              )
            )}
            {/* F-1: the unsupported combos (value_clv / contrarian / stale_line, deterministic) have NO
                distinct backend strategy. Rather than silently deploy momentum-sharp under a mismatched
                card name, deploy is DISABLED and the reason is shown — observable, never silent. */}
            {!running && deployUnsupported ? (
              <p className={styles.deployBlocked} data-testid="deploy-unsupported-note" role="note">
                Deploy unavailable: {unsupportedReason}. No agent is queued.
              </p>
            ) : null}
            {/* Honest pin affordance — NOT a fabricated hash (law_hash / Create-wizard ruling).
                Real evidence/score/manifest hashes appear on the Proof Card after the sealed run.
                SUPPRESSED while a preflight failure is showing: a fail-closed 422 pins NO instance
                server-side, so surfacing "Config pinned ✓" (whose tooltip reads "frozen at create")
                alongside the failure would falsely imply a successful create — the failure alert is
                the operative outcome. */}
            {!running && pinned && diffEntries.length === 0 && !preflightFailure ? (
              <p className={styles.pinnedOk} data-testid="config-pinned">
                <span className="mono">Config pinned ✓</span>{' '}
                <InfoTip label={GLOSSARY.config_pinned.label}>{GLOSSARY.config_pinned.definition}</InfoTip>
              </p>
            ) : null}
            {/* Server-owned deploy outcome: the real run_id (returned before the seal), or the NAMED
                fail-closed preflight failure. Neither is client-fabricated — the endpoint decides. */}
            {runId ? (
              <p className={styles.deployedOk} data-testid="deploy-run-id">
                Deployed · run <span className="mono">{runId}</span>
              </p>
            ) : null}
            {preflightFailure && preflightFailure.length > 0 ? (
              <div className={styles.deployError} data-testid="deploy-preflight-error" role="alert">
                <span className={styles.deployErrorLabel}>Preflight failed (fail-closed):</span>{' '}
                <span className="mono">{preflightFailure.join(', ')}</span>
              </div>
            ) : null}
            <p className={styles.note}>Config is frozen at run start. Mid-run edits create a new version/run (SEC-009).</p>
          </aside>
        </div>
      </div>
    </section>
  );
}
