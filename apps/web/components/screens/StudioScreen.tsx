'use client';
import { useMemo, useState, type ReactNode } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { availableModes, resolveMode, type StudioMode } from '@/lib/studio/coupling';
import { ARCHETYPES, SPORTS_ACTION_TYPES, type Archetype, type ExecutionMode, type SourceMode } from '@/lib/catalog';
import { STRATEGY_TEMPLATES, COMPLEXITY_LABEL, type StrategyTemplate } from '@/lib/studio/templates';
import { buildPreflightPreview, PREFLIGHT_DISCLAIMER } from '@/lib/studio/preflight';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';
import { GLOSSARY } from '@/lib/glossary';
import { deployAgent, DeployPreflightError, type DeployAgentPayload } from '@/lib/api';
import styles from './StudioScreen.module.css';

// The QuoteGuard/MM template id — the deploy discriminator for the MM family (decision 2). Driving
// the MM payload off the SELECTED TEMPLATE (not the archetype) is deliberate: quoteguard_mm reuses
// the `baseline` archetype, so mapping by archetype alone would hijack every manual baseline pick.
const MM_TEMPLATE_ID = 'quoteguard_mm';

// The MM tape catalog KEY (NOT a path/fixture). This key resolves a CLEARLY-LABELED SYNTHETIC /
// canned MM-mechanism replay tape via the server-side mm_tape_resolver seam — it is NOT a real
// market recording, and (A8 honesty) the key must NOT imply a real event provenance the tape lacks.
// fu-ii5-demo-tape registers this key into the backend catalog; until then a LIVE deploy fail-closes
// on mm_family, and the frontend tests mock fetch so they pass without the tape.
const DEMO_MM_TAPE_REF = 'synthetic-mm-mechanism-v1';

// Clamp any execution mode to what the MM family permits: the backend fail-closes MM on
// execution_mode == 'live_guarded', so the MM card only ever emits paper | dry_run.
function mmExecutionMode(exec: ExecutionMode): 'paper' | 'dry_run' {
  return exec === 'dry_run' ? 'dry_run' : 'paper';
}

// Map the Studio archetype + mode onto a backend strategy family (deploy launches this agent).
function toStrategy(archetype: Archetype, mode: StudioMode): string {
  if (mode === 'llm') return 'llm';
  if (archetype === 'momentum') return 'momentum-sharp';
  if (archetype === 'baseline') return 'baseline';
  return 'momentum-sharp';
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
function buildDeployPayload(
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
      market_allowlist: DEFAULT_POLICY_ENVELOPE.market_allowlist, // non-empty (empty → MM reject)
      venue_allowlist: DEFAULT_POLICY_ENVELOPE.venue_allowlist,
      min_edge_bps: DEFAULT_POLICY_ENVELOPE.min_edge_bps,
      max_stake: DEFAULT_POLICY_ENVELOPE.max_stake,
      window_id: `studio-${MM_TEMPLATE_ID}`,
      fixture_id: 1,
      end_rule: 'pre_match',
      mm: {
        tape_ref: DEMO_MM_TAPE_REF,
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
  return {
    template_id: archetype,
    agent_id: `studio-${archetype}`,
    strategy: toStrategy(archetype, mode),
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
}: { onPin?: () => void; running?: boolean }) {
  const [archetype, setArchetype] = useState<Archetype>('value_clv');
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
    { archetype: 'value_clv', mode: 'numeric', exec: 'paper' },
  );
  // Whether the current draft has been pinned (drives the honest "Config pinned ✓" affordance).
  const [pinned, setPinned] = useState(false);
  // Server-owned deploy state — the run_id the pinned instance launched, or the NAMED preflight
  // failure. There is NO client-fabricated deploy handle: the endpoint is the source of truth.
  const [runId, setRunId] = useState<string | null>(null);
  const [preflightFailure, setPreflightFailure] = useState<string[] | null>(null);
  const [deploying, setDeploying] = useState(false);

  const modes = availableModes(archetype);
  // fu-ii5: the MM template is restricted to the fail-closed-safe modes — the source/exec selectors
  // must not offer live / live_guarded for it (an operator cannot pick a mode the backend rejects).
  const isMM = templateId === MM_TEMPLATE_ID;

  // AC-007 snap-back: whenever archetype changes, re-resolve the current mode. A manual archetype/
  // mode edit clears the selected template — the deploy reverts to the directional path (fu-ii5).
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
      setExec((e) => (e === 'live_guarded' ? 'paper' : e));
    }
  }

  // #4 reviewable diff: current draft vs last-pinned baseline (a structured before→after patch).
  const diffEntries = [
    { field: 'archetype', before: baseline.archetype as string, after: archetype as string },
    { field: 'mode', before: baseline.mode as string, after: mode as string },
    { field: 'execution_mode', before: baseline.exec as string, after: exec as string },
  ].filter((e) => e.before !== e.after);

  async function pin() {
    onPin();
    setBaseline({ archetype, mode, exec }); // applied as the new pinned version
    setPinned(true);
    // DEPLOY for real: POST the config → fail-closed preflight → pinned instance + async run_id.
    // The run_id / named preflight failure are server-owned (no loose client-only deploy state).
    setDeploying(true);
    setPreflightFailure(null);
    setRunId(null);
    try {
      const result = await deployAgent(buildDeployPayload(archetype, mode, exec, source, templateId));
      setRunId(result.run_id);
    } catch (err) {
      if (err instanceof DeployPreflightError) setPreflightFailure(err.failedChecks);
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
                // fu-ii5: a heavy extension is LOCKED unless it is per-template `deployable`. Only
                // quoteguard_mm is deployable, so Arb/Spread stays Phase-3-locked and the MM card
                // becomes selectable with an HONEST label (no "Phase-3", no implied live trading).
                const locked = t.complexity === 'heavy-extension' && !t.deployable;
                // HONEST source label: the deployable MM card is a SIMULATED / SYNTHETIC REPLAY of a
                // canned MM-mechanism fixture — never live TxLINE, never a real match. No "live",
                // no "genuine", no event/team branding.
                const complexityLabel = t.deployable
                  ? 'synthetic replay (simulated) · dry-run · live-money disabled'
                  : COMPLEXITY_LABEL[t.complexity];
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
                    <span className={styles.cardBlurb}>{t.blurb}</span>
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

            {/* HONEST provenance for the MM family: the queued run is a SIMULATED synthetic replay of
                a canned MM-mechanism fixture — never live TxLINE, never a real match/team. */}
            {isMM ? (
              <p className={styles.hint} data-testid="mm-synthetic-note">
                QuoteGuard/MM runs a SIMULATED synthetic replay of a canned MM-mechanism fixture —
                no live TxLINE, no real match, no real-money execution.
              </p>
            ) : null}

            {running ? (
              <p className={styles.roBanner} data-testid="run-readonly">Config is read-only during a scored run (SEC-006). Edits create a new version, never a live mutation.</p>
            ) : (
              <button type="button" className={styles.pin} onClick={pin} disabled={deploying}>
                {deploying ? 'DEPLOYING…' : 'PIN CONFIG & QUEUE RUN →'}
              </button>
            )}
            {/* Honest pin affordance — NOT a fabricated hash (law_hash / Create-wizard ruling).
                Real evidence/score/manifest hashes appear on the Proof Card after the sealed run. */}
            {!running && pinned && diffEntries.length === 0 ? (
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
