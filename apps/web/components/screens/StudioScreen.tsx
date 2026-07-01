'use client';
import { useMemo, useState, type ReactNode } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { InfoTip } from '@/components/ui/InfoTip';
import { availableModes, resolveMode, type StudioMode } from '@/lib/studio/coupling';
import { ARCHETYPES, SPORTS_ACTION_TYPES, type Archetype, type ExecutionMode } from '@/lib/catalog';
import { STRATEGY_TEMPLATES, COMPLEXITY_LABEL, type StrategyTemplate } from '@/lib/studio/templates';
import { buildPreflightPreview, PREFLIGHT_DISCLAIMER } from '@/lib/studio/preflight';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';
import { GLOSSARY } from '@/lib/glossary';
import styles from './StudioScreen.module.css';

// Deterministic, fiction config hashes derived from the chosen config (pre-run pin).
function hashConfig(a: Archetype, mode: StudioMode): string {
  return `0xcfg_${a}_${mode}`.slice(0, 22);
}
function hashPolicy(minEdge: number, exec: ExecutionMode): string {
  return `0xpol_${minEdge}_${exec}`.slice(0, 22);
}

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
}: { onPin?: (pinned: { config_hash: string; policy_hash: string }) => void; running?: boolean }) {
  const [archetype, setArchetype] = useState<Archetype>('value_clv');
  const [mode, setMode] = useState<StudioMode>('numeric');
  const [exec, setExec] = useState<ExecutionMode>('paper');
  // The last-pinned snapshot — edits are diffed against it and applied as a NEW version on pin.
  const [baseline, setBaseline] = useState<{ archetype: Archetype; mode: StudioMode; exec: ExecutionMode }>(
    { archetype: 'value_clv', mode: 'numeric', exec: 'paper' },
  );

  const modes = availableModes(archetype);

  // AC-007 snap-back: whenever archetype changes, re-resolve the current mode.
  function onArchetype(next: Archetype) {
    setArchetype(next);
    setMode((m) => resolveMode(next, m));
  }
  function onMode(next: StudioMode) {
    setMode(resolveMode(archetype, next));
  }
  // Selecting a BUILT template applies its archetype + default mode through the existing coupling
  // (snap-back preserved). Heavy-extension (Phase-3) templates are not selectable.
  function applyTemplate(t: StrategyTemplate) {
    setArchetype(t.archetype);
    setMode(resolveMode(t.archetype, t.defaultMode));
  }

  const config_hash = hashConfig(archetype, mode);
  const policy_hash = hashPolicy(DEFAULT_POLICY_ENVELOPE.min_edge_bps, exec);

  // #4 reviewable diff: current draft vs last-pinned baseline (a structured before→after patch).
  const diffEntries = [
    { field: 'archetype', before: baseline.archetype as string, after: archetype as string },
    { field: 'mode', before: baseline.mode as string, after: mode as string },
    { field: 'execution_mode', before: baseline.exec as string, after: exec as string },
  ].filter((e) => e.before !== e.after);

  function pin() {
    onPin({ config_hash, policy_hash });
    setBaseline({ archetype, mode, exec }); // applied as the new pinned version
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
                const phase3 = t.complexity === 'heavy-extension';
                return (
                  <button
                    key={t.id}
                    type="button"
                    className={`${styles.card} ${phase3 ? styles.cardPhase3 : ''}`}
                    disabled={phase3 || running}
                    aria-disabled={phase3 || undefined}
                    onClick={() => { if (!phase3) applyTemplate(t); }}
                  >
                    <span className={styles.cardLabel}>{t.label}</span>
                    <span className={styles.cardComplexity}>{COMPLEXITY_LABEL[t.complexity]}</span>
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
                Execution mode{' '}
                <InfoTip label={GLOSSARY.execution_mode.label}>{GLOSSARY.execution_mode.definition}</InfoTip>
              </span>
              {running ? (
                <span className="mono">{exec}</span>
              ) : (
                <SegmentedControl<ExecutionMode>
                  ariaLabel="Execution mode" value={exec} onChange={setExec}
                  options={[{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }, { value: 'live_guarded', label: 'Live Guarded' }]}
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

            {running ? (
              <p className={styles.roBanner} data-testid="run-readonly">Config is read-only during a scored run (SEC-006). Edits create a new version, never a live mutation.</p>
            ) : (
              <button type="button" className={styles.pin} onClick={pin}>
                PIN CONFIG &amp; QUEUE RUN →
              </button>
            )}
            <p className={styles.note}>Config is frozen at run start. Mid-run edits create a new version/run (SEC-009).</p>
          </aside>
        </div>
      </div>
    </section>
  );
}
