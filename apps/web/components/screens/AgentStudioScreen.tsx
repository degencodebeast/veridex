'use client';
import { useMemo, useState, type ReactNode } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { JsonView } from '@/components/ui/JsonView';
import { Badge } from '@/components/ui/Badge';
import { availableModes, resolveMode, type StudioMode } from '@/lib/studio/coupling';
import { ARCHETYPES, SPORTS_ACTION_TYPES, type Archetype, type ExecutionMode } from '@/lib/catalog';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';
import styles from './AgentStudioScreen.module.css';

// Deterministic, fiction config hashes derived from the chosen config (pre-run pin).
function hashConfig(a: Archetype, mode: StudioMode): string {
  return `0xcfg_${a}_${mode}`.slice(0, 22);
}
function hashPolicy(minEdge: number, exec: ExecutionMode): string {
  return `0xpol_${minEdge}_${exec}`.slice(0, 22);
}

function Section({ n, title, inactive, children }: { n: string; title: string; inactive?: boolean; children: ReactNode }) {
  return (
    <section className={`${styles.section} ${inactive ? styles.inactive : ''}`} data-testid={`section-${n}`} data-inactive={inactive ? 'true' : 'false'}>
      <h2 className={styles.h2}><span className={styles.num}>{n}</span> {title}</h2>
      {inactive ? <p className={styles.stub}>Not applicable in this mode.</p> : children}
    </section>
  );
}

export function AgentStudioScreen({
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

  const config_hash = hashConfig(archetype, mode);
  const policy_hash = hashPolicy(DEFAULT_POLICY_ENVELOPE.min_edge_bps, exec);
  const sampleEdge = 14.0;
  const policyDecision = sampleEdge >= DEFAULT_POLICY_ENVELOPE.min_edge_bps ? 'ALLOW' : 'DENY';

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

  const previewConfig = useMemo(() => ({
    archetype, mode, market_scope: DEFAULT_POLICY_ENVELOPE.market_allowlist,
    min_edge_bps: DEFAULT_POLICY_ENVELOPE.min_edge_bps, execution_mode: exec,
    config_hash, policy_hash,
  }), [archetype, mode, exec, config_hash, policy_hash]);

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
            <label className={styles.field}>
              <span className={styles.label}>Archetype</span>
              <select aria-label="Archetype" className={styles.select} value={archetype} disabled={running} onChange={(e) => onArchetype(e.target.value as Archetype)}>
                {ARCHETYPES.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <p className={styles.hint}>Source: STUDIO (reproducible). BYOA agents are verified, not reproducible (Phase-3).</p>
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
            <div className={styles.kv}><span>max_stake</span><span className="mono">{DEFAULT_POLICY_ENVELOPE.max_stake}</span></div>
            <div className={styles.kv}><span>kill_switch</span><span className="mono">{String(DEFAULT_POLICY_ENVELOPE.kill_switch)}</span></div>
            <label className={styles.field}>
              <span className={styles.label}>Execution mode</span>
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

          <aside className={styles.preflight} data-testid="preflight">
            <h2 className={styles.h2}>Preflight Preview</h2>
            <p className={styles.plain}>
              {mode === 'llm' ? 'LLM shell' : mode === 'numeric' ? 'Numeric strategy' : 'Rule-based automation'} · archetype {archetype} · scope {DEFAULT_POLICY_ENVELOPE.market_allowlist.join(', ')}.
            </p>
            <div className={styles.previewRow}>
              <span className={styles.previewLabel}>Recomputed edge (sample)</span>
              <span className={`mono ${styles.edge}`}>+{sampleEdge.toFixed(1)} bps</span>
            </div>
            <div className={styles.previewRow}>
              <span className={styles.previewLabel}>Policy</span>
              <Badge variant={policyDecision === 'ALLOW' ? 'valid' : 'invalid'}>{policyDecision}</Badge>
            </div>
            <JsonView data={previewConfig} />
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
