'use client';
import { useEffect, useMemo, useState } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { Badge } from '@/components/ui/Badge';
import { InfoTip } from '@/components/ui/InfoTip';
import { FIXTURES } from '@/lib/fixtures/catalog';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/config/policy';
import { MARKET_FAMILY_KEYS } from '@/lib/catalog';
import { GLOSSARY } from '@/lib/glossary';
import { shortHash } from '@/lib/format';
import {
  getInstances, createCompetition, registerRosterAgent, startCompetition,
  type DeployedInstance, type CompetitionConfigPayload, type RosterEntryPayload,
} from '@/lib/api';
import type { CompetitionType, ExecutionMode, ProofMode, MarketFamilyKey } from '@/lib/catalog';
import styles from './CreateCompetitionScreen.module.css';

type SourceMode = 'replay' | 'live';

function proofFor(type: CompetitionType, source: SourceMode): ProofMode {
  if (source === 'replay' || type === 'replay_arena') return 'reproducible';
  return 'verified';
}

// A deployed instance is roster-eligible once it has a pinned identity to run — i.e. it reached a
// running/sealed lifecycle state. pending/failed instances have no runnable pinned config and are
// therefore not listable (honest: never offered as a contestant that couldn't actually run).
const ELIGIBLE_STATUS = new Set(['running', 'sealed']);

// The 4 real competition_type enum values (veridex CompetitionConfig) as rich cards. MAJOR-1: the
// live_arena card is gated closed — a Live Arena needs a live TxLINE feed, which is not wired yet, and
// the backend runs a recorded tape. Its blurb makes NO "live / real-time" claim (that would be
// dishonest over a forced-replay run); the honest no-feed note replaces it and the card is disabled.
const TYPE_CARDS: { type: CompetitionType; label: string; blurb: string; disabled?: boolean }[] = [
  { type: 'live_arena', label: 'Live Arena', blurb: 'A live TxLINE feed is not wired yet, so a live arena is unavailable — runs use a recorded replay.', disabled: true },
  { type: 'replay_arena', label: 'Replay Arena', blurb: 'Deterministic replay of a recorded fixture window.' },
  { type: 'head_to_head', label: 'Head-to-Head', blurb: 'Two agents, identical evidence — CLV gap, no winner badge.' },
  { type: 'prize_vault_challenge', label: 'Prize-Vault Challenge', blurb: 'Designed prize target (Phase 2D · no funds move).' },
];

// Short labels for the REAL market families (never invented markets) → composed into market_scope.
const MARKET_LABEL: Record<MarketFamilyKey, string> = {
  '1X2_PARTICIPANT_RESULT': '1X2',
  'OVERUNDER_PARTICIPANT_GOALS': 'O/U',
  'ASIANHANDICAP_PARTICIPANT_GOALS': 'AH',
};

export interface CreateCompetitionCommit {
  competition_type: CompetitionType;
  source_mode: SourceMode;
  execution_mode: ExecutionMode;
  // The wizard commits EXACTLY the fields POST /competitions freezes into CompetitionConfig
  // (SEC-009 "frozen at entry"): type/source/exec + market_scope + scoring_window. proof_mode is
  // deterministic from type+source and travels in the pinned block.
  market_scope: string;
  scoring_window: string | null;
  proof_mode: ProofMode;
}

/** Injectable launch API (defaults to the real client) — one seam so tests drive the flow offline. */
export interface LaunchApi {
  create: (config: CompetitionConfigPayload) => Promise<{ competition_id: string; status: string }>;
  register: (competitionId: string, entry: RosterEntryPayload) => Promise<unknown>;
  start: (competitionId: string) => Promise<{ competition_id: string; status: string; run_id: string | null }>;
}

const DEFAULT_LAUNCH_API: LaunchApi = {
  create: createCompetition,
  register: registerRosterAgent,
  start: startCompetition,
};

export function CreateCompetitionScreen({
  onCommit = () => {},
  initialFixtureId,
  connected = false,
  onConnect,
  loadInstances = getInstances,
  launchApi = DEFAULT_LAUNCH_API,
  onLaunched,
}: {
  onCommit?: (cfg: CreateCompetitionCommit) => void;
  initialFixtureId?: number;
  // auth-contract@1: DERIVED from the real session by the page (usePrivy), never a literal. Owner-
  // scoped instance listing + the launch POSTs only fire authenticated (fail-closed).
  connected?: boolean;
  onConnect?: () => void;
  loadInstances?: () => Promise<DeployedInstance[]>;
  launchApi?: LaunchApi;
  onLaunched?: (competitionId: string) => void;
}) {
  // MAJOR-1 honesty: default to a REPLAY competition. The backend `start` runs a recorded tape
  // (build_demo_ticks) unconditionally and only echoes source_mode, so a "live" default would ship a
  // tape mislabeled live end-to-end. Default replay + gate the Live source option (below) until a real
  // live feed is wired — a tape run must never be presented as live.
  const [type, setType] = useState<CompetitionType>('replay_arena');
  const [source, setSource] = useState<SourceMode>('replay');
  const [exec, setExec] = useState<ExecutionMode>('paper');
  const [fixtureId, setFixtureId] = useState<number>(
    FIXTURES.some((f) => f.fixture_id === initialFixtureId) ? (initialFixtureId as number) : (FIXTURES[0]?.fixture_id ?? 0),
  );
  const [markets, setMarkets] = useState<Set<MarketFamilyKey>>(new Set(MARKET_FAMILY_KEYS));
  const [scoringWindow, setScoringWindow] = useState('');

  // ── Roster (F-4): the operator's OWN eligible deployed instances (owner-scoped, bearer-authed).
  // Load ONLY when authenticated; it never falls back to a fixture — an empty/failed load renders an
  // honest empty/error state (T-2 fixture prohibition), so a fixture can never masquerade as a real
  // deployment. `selected` holds instance ids; the roster payload is instance-bound off the record.
  const [instState, setInstState] = useState<
    | { kind: 'idle' } | { kind: 'loading' } | { kind: 'error' } | { kind: 'ready'; instances: DeployedInstance[] }
  >(() => ({ kind: 'idle' }));
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!connected) { setInstState({ kind: 'idle' }); return; }
    let active = true;
    setInstState({ kind: 'loading' });
    loadInstances()
      .then((instances) => { if (active) setInstState({ kind: 'ready', instances }); })
      .catch(() => { if (active) setInstState({ kind: 'error' }); });
    return () => { active = false; };
  }, [connected, loadInstances]);

  const proof = proofFor(type, source);
  const selectedFixture = FIXTURES.find((f) => f.fixture_id === fixtureId) ?? FIXTURES[0];
  const marketKeys = MARKET_FAMILY_KEYS.filter((k) => markets.has(k));
  const fixtureScope = selectedFixture ? `${selectedFixture.participant1} v ${selectedFixture.participant2}` : '';
  // market_scope is the single free-form selector POST accepts (e.g. "FRA v BRA · 1X2 / O/U / AH").
  const market_scope = [fixtureScope, marketKeys.map((k) => MARKET_LABEL[k]).join(' / ')].filter(Boolean).join(' · ');
  const scoring_window = scoringWindow.trim() || null;

  const eligible = instState.kind === 'ready' ? instState.instances.filter((i) => ELIGIBLE_STATUS.has(i.status)) : [];
  const selectedInstances = useMemo(() => eligible.filter((i) => selected.has(i.instance_id)), [eligible, selected]);

  const toggleMarket = (k: MarketFamilyKey) =>
    setMarkets((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });

  const toggleInstance = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  const commit: CreateCompetitionCommit = {
    competition_type: type, source_mode: source, execution_mode: exec,
    market_scope, scoring_window, proof_mode: proof,
  };

  // The exact CompetitionConfig POST /competitions freezes. roster_size = selected count (≥2 guarded
  // by the launch button). source_mode is the wizard's own axis — a replay-backed run reads `replay`.
  const config: CompetitionConfigPayload = {
    competition_type: type, source_mode: source, execution_mode: exec,
    market_scope, scoring_window, roster_size: Math.max(2, selectedInstances.length),
  };

  return (
    <section className={styles.screen} aria-label="Create Competition">
      <h1 className={styles.title}>Create Competition</h1>
      <div className={styles.layout}>
        <div className={styles.main}>
          {/* 01 — TYPE (rich cards) + source/exec axes */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>01</span> Type &amp; mode</h2>
            <div className={styles.cards} data-testid="type-cards">
              {TYPE_CARDS.map((c) => (
                <button
                  key={c.type}
                  type="button"
                  data-testid={`type-${c.type}`}
                  aria-pressed={c.disabled ? undefined : type === c.type}
                  disabled={c.disabled}
                  className={`${styles.card} ${type === c.type ? styles.cardActive : ''} ${c.disabled ? styles.cardLocked : ''}`}
                  onClick={() => { if (!c.disabled) setType(c.type); }}
                >
                  <span className={styles.cardLabel}>{c.label}{c.disabled ? ' 🔒' : ''}</span>
                  <span className={styles.cardBlurb} data-testid={c.disabled ? 'type-live-note' : undefined}>{c.blurb}</span>
                </button>
              ))}
            </div>
            <div className={styles.controls}>
              {/* A <div> (not <label>): a label must not wrap a radiogroup — the group carries its own
                  aria-label, and wrapping would fold the label + note text into the first radio's
                  accessible name. */}
              <div className={styles.field}>
                <span className={styles.label}>Source</span>
                <SegmentedControl<SourceMode>
                  ariaLabel="Source mode" value={source} onChange={setSource}
                  options={[{ value: 'replay', label: 'Replay' }, { value: 'live', label: 'Live', locked: true }]}
                />
                {/* MAJOR-1: Live is gated closed (fail-closed, mirroring the deploy path) — there is no
                    live TxLINE feed wired, so every run is a recorded replay. Never offer a selectable
                    "live" source that silently runs the demo tape. */}
                <span className={styles.sourceNote} data-testid="source-live-note">
                  Live is unavailable — no live TxLINE feed is wired yet, so every run uses a recorded replay.
                </span>
              </div>
              <label className={styles.field}>
                <span className={styles.label}>Execution</span>
                <SegmentedControl<ExecutionMode>
                  ariaLabel="Execution mode" value={exec} onChange={setExec}
                  options={[{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }, { value: 'live_guarded', label: 'Live Guarded' }]}
                />
              </label>
            </div>
          </section>

          {/* 02 — FIXTURE & SCORING WINDOW */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>02</span> Fixture &amp; scoring window</h2>
            <div className={styles.controls}>
              <label className={styles.field}>
                <span className={styles.label}>Fixture</span>
                <select
                  className={styles.select} aria-label="Fixture" data-testid="fixture-select"
                  value={fixtureId} onChange={(e) => setFixtureId(Number(e.target.value))}
                >
                  {FIXTURES.map((f) => (
                    <option key={f.fixture_id} value={f.fixture_id}>{f.participant1} v {f.participant2} · {f.competition}</option>
                  ))}
                </select>
              </label>
              <label className={styles.field}>
                <span className={styles.label}>Scoring window (optional)</span>
                <input
                  className={styles.input} type="text" data-testid="scoring-window"
                  placeholder="ISO-8601 duration, e.g. PT90M — blank = full match"
                  value={scoringWindow} onChange={(e) => setScoringWindow(e.target.value)}
                />
              </label>
            </div>
          </section>

          {/* 03 — MARKET SCOPE (real families only) */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>03</span> Market scope</h2>
            <div className={styles.checks} data-testid="market-scope">
              {MARKET_FAMILY_KEYS.map((k) => (
                <label key={k} className={styles.check} data-testid={`market-${k}`}>
                  <input type="checkbox" checked={markets.has(k)} onChange={() => toggleMarket(k)} aria-label={MARKET_LABEL[k]} />
                  <span>{MARKET_LABEL[k]}</span>
                </label>
              ))}
            </div>
          </section>

          {/* 04 — ROSTER (F-4): owner-scoped selector of eligible deployed instances (the shipped
              wizard has no separate proof/exec section, so ROSTER is section 04, not 05) */}
          <RosterSection
            connected={connected}
            state={instState}
            eligible={eligible}
            selected={selected}
            onToggle={toggleInstance}
            proofFor={(inst) => (inst.source_mode === 'live' ? 'verified' : 'reproducible')}
          />
        </div>

        {/* SUMMARY sidebar — the real CompetitionConfig POST /competitions freezes (SEC-009). */}
        <aside className={styles.pinned} data-testid="pinned-config" aria-label="Pinned configuration">
          <h2 className={styles.h2}>Pinned before entry</h2>
          <dl className={styles.summary}>
            <div className={styles.sumRow}><dt>TYPE</dt><dd data-testid="summary-type">{TYPE_CARDS.find((c) => c.type === type)?.label}</dd></div>
            <div className={styles.sumRow}><dt>SOURCE <InfoTip label={GLOSSARY.source_mode.label}>{GLOSSARY.source_mode.definition}</InfoTip></dt><dd data-testid="summary-source"><Badge variant={source === 'live' ? 'live' : 'replay'} /></dd></div>
            <div className={styles.sumRow}><dt>EXEC <InfoTip label={GLOSSARY.execution_mode.label}>{GLOSSARY.execution_mode.definition}</InfoTip></dt><dd data-testid="summary-exec" className="mono">{exec}</dd></div>
            <div className={styles.sumRow}><dt>MARKET SCOPE</dt><dd data-testid="summary-market-scope" className="mono">{market_scope || '—'}</dd></div>
            <div className={styles.sumRow}><dt>SCORING WINDOW</dt><dd data-testid="summary-scoring-window" className="mono">{scoring_window ?? 'full match'}</dd></div>
            <div className={styles.sumRow}><dt>ROSTER</dt><dd data-testid="summary-roster" className="mono">{selectedInstances.length} {selectedInstances.length === 1 ? 'agent' : 'agents'}</dd></div>
            <div className={styles.sumRow}><dt>PROOF <InfoTip label={GLOSSARY.proof_mode.label}>{GLOSSARY.proof_mode.definition}</InfoTip></dt><dd><Badge variant={proof} /></dd></div>
            {/* law_hash is NOT a digest at create (POST surfaces none — config_hash is per-agent at
                registration, policy_hash at run-start). Honest pin = the config itself. */}
            <div className={styles.sumRow}><dt>CONFIG <InfoTip label={GLOSSARY.config_pinned.label}>{GLOSSARY.config_pinned.definition}</InfoTip></dt><dd data-testid="summary-config-pinned" className="mono">Config pinned ✓</dd></div>
            <div className={styles.sumRow}><dt>POLICY</dt><dd className="mono">min_edge {DEFAULT_POLICY_ENVELOPE.min_edge_bps} bps · kill {String(DEFAULT_POLICY_ENVELOPE.kill_switch)}</dd></div>
          </dl>
          <p className={styles.pinnedCaption} data-testid="config-pinned-caption">This exact CompetitionConfig is frozen at create. Run evidence, score roots, and manifest hashes appear on the Proof Card after the run.</p>
          <div className={styles.pins}>
            <span className={styles.pin}>LAW deterministic recompute</span>
          </div>
          <p className={styles.note}>These are frozen at entry. Scoring law, source mode, roster &amp; execution mode are frozen for the run at start; changing config after a run starts creates a new version (SEC-009).</p>

          {/* Launch progression (F-4): create → register roster → start, with per-instance partial
              failure + retry. Fires onCommit with the pinned config, then the real POSTs. */}
          <LaunchPanel
            config={config}
            roster={selectedInstances}
            connected={connected}
            onConnect={onConnect}
            noEligible={instState.kind === 'ready' && eligible.length === 0}
            api={launchApi}
            onBeforeLaunch={() => onCommit(commit)}
            onLaunched={onLaunched}
          />
        </aside>
      </div>
    </section>
  );
}

// ── Section 04 · ROSTER ─────────────────────────────────────────────────────────────────────────
function RosterSection({
  connected, state, eligible, selected, onToggle, proofFor: proofForInstance,
}: {
  connected: boolean;
  state: { kind: 'idle' } | { kind: 'loading' } | { kind: 'error' } | { kind: 'ready'; instances: DeployedInstance[] };
  eligible: DeployedInstance[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  proofFor: (inst: DeployedInstance) => ProofMode;
}) {
  return (
    <section className={styles.section} data-testid="roster-section">
      <h2 className={styles.h2}><span className={styles.n}>04</span> Roster</h2>
      {!connected && (
        <p className={styles.rosterEmpty} data-testid="roster-auth">Connect wallet to list your instances.</p>
      )}
      {connected && state.kind === 'loading' && (
        <p className={styles.rosterEmpty} data-testid="roster-loading">Loading your instances…</p>
      )}
      {connected && state.kind === 'error' && (
        <p className={styles.rosterEmpty} data-testid="roster-error">Couldn&apos;t load your instances. Check your session and try again.</p>
      )}
      {connected && state.kind === 'ready' && eligible.length === 0 && (
        <p className={styles.rosterEmpty} data-testid="roster-no-eligible">No eligible instances. Deploy an agent in Studio first.</p>
      )}
      {connected && state.kind === 'ready' && eligible.length > 0 && (
        <>
          <ul className={styles.rosterList}>
            {eligible.map((inst) => {
              const isSel = selected.has(inst.instance_id);
              return (
                <li key={inst.instance_id}>
                  <button
                    type="button"
                    className={`${styles.rosterRow} ${isSel ? styles.rosterRowSel : ''}`}
                    aria-pressed={isSel}
                    data-testid={`roster-${inst.instance_id}`}
                    onClick={() => onToggle(inst.instance_id)}
                  >
                    <span className={`${styles.rosterCheck} ${isSel ? styles.rosterCheckOn : ''}`} aria-hidden>{isSel ? '✓' : ''}</span>
                    <span className={styles.rosterMain}>
                      <span className={styles.rosterIdentity}>
                        <span className={styles.rosterName}>{inst.agent_id}</span>
                        <span className={styles.rosterStrategy}>{inst.template_id}</span>
                        <Badge variant={proofForInstance(inst)} />
                      </span>
                      {/* Every field is from the real instance record — never fabricated. */}
                      <span className={`${styles.rosterMeta} mono`}>
                        inst {inst.instance_id} · cfg:{shortHash(inst.config_hash)} · {inst.source_mode}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <p className={styles.rosterHint}>
            Only <strong>your</strong> deployed, eligible instances are listable. Contestant identity, strategy type, proof/source mode &amp; pinned config are shown from the instance record — never fabricated.
          </p>
        </>
      )}
    </section>
  );
}

// ── Launch progression (F-4): create → register roster → start ──────────────────────────────────
type RegStatus = 'pending' | 'ok' | 'failed';
type LaunchState =
  | { phase: 'idle' }
  | { phase: 'creating' }
  | { phase: 'registering'; competitionId: string; statuses: Record<string, RegStatus> }
  | { phase: 'partial'; competitionId: string; statuses: Record<string, RegStatus> }
  | { phase: 'starting'; competitionId: string; statuses: Record<string, RegStatus> }
  | { phase: 'started'; competitionId: string; runId: string | null }
  | { phase: 'error'; at: 'create' | 'start'; competitionId?: string; statuses?: Record<string, RegStatus>; message: string };

function rosterEntry(inst: DeployedInstance): RosterEntryPayload {
  return {
    agent_id: inst.agent_id,
    owner: inst.operator_id ?? '', // server derives the real owner from the principal; echo only
    strategy: inst.template_id,
    model: null,
    proof_mode: inst.source_mode === 'live' ? 'verified' : 'reproducible', // backend re-normalises
    config_hash: inst.config_hash, // pins the instance's config identity (I-7 roster→instance bind)
    execution_eligibility: false,
    instance_id: inst.instance_id, // THE binding — the arena runs this deployed contestant
  };
}

function okCount(statuses: Record<string, RegStatus>): number {
  return Object.values(statuses).filter((s) => s === 'ok').length;
}

function LaunchPanel({
  config, roster, connected, onConnect, noEligible, api, onBeforeLaunch, onLaunched,
}: {
  config: CompetitionConfigPayload;
  roster: DeployedInstance[];
  connected: boolean;
  onConnect?: () => void;
  noEligible: boolean;
  api: LaunchApi;
  onBeforeLaunch: () => void;
  onLaunched?: (competitionId: string) => void;
}) {
  const [state, setState] = useState<LaunchState>({ phase: 'idle' });
  const busy = state.phase === 'creating' || state.phase === 'registering' || state.phase === 'starting';
  const canLaunch = connected && roster.length >= 2 && !busy && state.phase !== 'started';

  // Register a set of instances one at a time, updating per-instance status. Each failure is caught
  // and marked `failed` (never aborts the batch), so a partial roster surfaces exactly which ones
  // failed — the launch never fabricates a success for an instance the backend refused.
  async function registerBatch(competitionId: string, toRegister: DeployedInstance[], base: Record<string, RegStatus>) {
    const statuses: Record<string, RegStatus> = { ...base };
    for (const inst of toRegister) statuses[inst.instance_id] = 'pending';
    setState({ phase: 'registering', competitionId, statuses });
    for (const inst of toRegister) {
      try {
        await api.register(competitionId, rosterEntry(inst));
        statuses[inst.instance_id] = 'ok';
      } catch {
        statuses[inst.instance_id] = 'failed';
      }
      setState({ phase: 'registering', competitionId, statuses: { ...statuses } });
    }
    return statuses;
  }

  async function runStart(competitionId: string, statuses: Record<string, RegStatus>) {
    setState({ phase: 'starting', competitionId, statuses });
    try {
      const res = await api.start(competitionId);
      setState({ phase: 'started', competitionId, runId: res.run_id });
      onLaunched?.(res.competition_id || competitionId);
    } catch (err) {
      setState({ phase: 'error', at: 'start', competitionId, statuses, message: err instanceof Error ? err.message : 'start failed' });
    }
  }

  async function launch() {
    if (!canLaunch) return;
    onBeforeLaunch(); // notify: commit EXACTLY the pinned config (SEC-009), then the real POSTs
    setState({ phase: 'creating' });
    let competitionId: string;
    try {
      const created = await api.create(config);
      competitionId = created.competition_id;
    } catch (err) {
      setState({ phase: 'error', at: 'create', message: err instanceof Error ? err.message : 'create failed' });
      return;
    }
    const statuses = await registerBatch(competitionId, roster, {});
    if (Object.values(statuses).every((s) => s === 'ok')) {
      await runStart(competitionId, statuses);
    } else {
      setState({ phase: 'partial', competitionId, statuses });
    }
  }

  async function retryFailed(competitionId: string, statuses: Record<string, RegStatus>) {
    const failedInsts = roster.filter((i) => statuses[i.instance_id] === 'failed');
    const next = await registerBatch(competitionId, failedInsts, statuses);
    if (Object.values(next).every((s) => s === 'ok')) await runStart(competitionId, next);
    else setState({ phase: 'partial', competitionId, statuses: next });
  }

  return (
    <div className={styles.launch} data-testid="launch-panel">
      <button
        type="button"
        className={styles.launchBtn}
        data-testid="launch-button"
        disabled={!canLaunch}
        onClick={launch}
      >
        Create · Register roster · Start →
      </button>

      {!connected && (
        <div className={styles.launchChip} data-testid="launch-auth">
          <span className={styles.launchChipGlyph}>⚿</span>
          <span>Connect wallet to list your instances.</span>
          {onConnect && <button type="button" className={styles.launchConnect} onClick={onConnect}>Connect</button>}
        </div>
      )}
      {connected && noEligible && (
        <div className={styles.launchChip} data-testid="launch-no-eligible">
          <span className={styles.launchChipGlyph}>○</span>
          <span>No eligible instances — deploy an agent in Studio first.</span>
        </div>
      )}
      {connected && !noEligible && roster.length < 2 && state.phase === 'idle' && (
        <p className={styles.launchHint} data-testid="launch-need-two">Select at least 2 eligible instances to launch a competition.</p>
      )}

      {state.phase !== 'idle' && (
        <div className={styles.progress} data-testid="launch-progress">
          <div className={styles.progressHead}>LAUNCH PROGRESSION</div>
          <ol className={styles.steps}>
            <Step label="create competition" status={createStepStatus(state)} />
            <Step label={registerStepLabel(state)} status={registerStepStatus(state)} />
            <Step label="start run" status={startStepStatus(state)} />
          </ol>

          {state.phase === 'partial' && (
            <div className={styles.partial} data-testid="launch-partial">
              <div className={styles.partialTitle}>⚠ partial: {failedNames(roster, state.statuses)} failed to register</div>
              <p className={styles.partialNote}>
                {okCount(state.statuses)} registered. Retry the failed {failedList(roster, state.statuses).length === 1 ? 'one' : 'ones'}, or start with the rest.
              </p>
              <div className={styles.partialActions}>
                <button type="button" className={styles.partialRetry} data-testid="launch-retry" onClick={() => retryFailed(state.competitionId, state.statuses)}>RETRY</button>
                {okCount(state.statuses) >= 2 && (
                  <button type="button" className={styles.partialStart} data-testid="launch-start-rest" onClick={() => runStart(state.competitionId, state.statuses)}>
                    START WITH {okCount(state.statuses)}
                  </button>
                )}
              </div>
            </div>
          )}

          {state.phase === 'error' && (
            <div className={styles.launchError} data-testid="launch-error">
              <div className={styles.partialTitle}>✕ {state.at === 'create' ? 'create' : 'start'} failed</div>
              <p className={styles.partialNote}>{state.message}. Nothing was fabricated — no run was started.</p>
              {state.at === 'start' && state.competitionId && state.statuses && (
                <div className={styles.partialActions}>
                  <button type="button" className={styles.partialRetry} data-testid="launch-retry-start" onClick={() => runStart(state.competitionId!, state.statuses!)}>RETRY START</button>
                </div>
              )}
            </div>
          )}

          {state.phase === 'started' && (
            <p className={styles.started} data-testid="launch-started">
              Started ✓ — {state.runId ? <>run <span className="mono">{state.runId}</span></> : 'awaiting run id'}. Opening arena…
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function failedList(roster: DeployedInstance[], statuses: Record<string, RegStatus>): DeployedInstance[] {
  return roster.filter((i) => statuses[i.instance_id] === 'failed');
}
function failedNames(roster: DeployedInstance[], statuses: Record<string, RegStatus>): string {
  return failedList(roster, statuses).map((i) => i.agent_id).join(', ') || 'an instance';
}

// step-status derivations — each step reflects the REAL phase, never an optimistic tick.
function createStepStatus(s: LaunchState): StepStatus {
  if (s.phase === 'creating') return 'active';
  if (s.phase === 'error' && s.at === 'create') return 'failed';
  if (s.phase === 'idle') return 'queued';
  return 'done';
}
function registerStepLabel(s: LaunchState): string {
  if ((s.phase === 'registering' || s.phase === 'partial' || s.phase === 'starting') && 'statuses' in s) {
    const total = Object.keys(s.statuses).length;
    return `register roster (${okCount(s.statuses)}/${total})`;
  }
  return 'register roster';
}
function registerStepStatus(s: LaunchState): StepStatus {
  if (s.phase === 'registering') return 'active';
  if (s.phase === 'partial') return 'failed';
  if (s.phase === 'starting' || s.phase === 'started') return 'done';
  if (s.phase === 'error' && s.at === 'start') return 'done';
  return 'queued';
}
function startStepStatus(s: LaunchState): StepStatus {
  if (s.phase === 'starting') return 'active';
  if (s.phase === 'started') return 'done';
  if (s.phase === 'error' && s.at === 'start') return 'failed';
  return 'queued';
}

type StepStatus = 'queued' | 'active' | 'done' | 'failed';
function Step({ label, status }: { label: string; status: StepStatus }) {
  const glyph = status === 'done' ? '✓' : status === 'failed' ? '✕' : status === 'active' ? '•' : '';
  return (
    <li className={styles.step} data-status={status}>
      <span className={`${styles.stepDot} ${styles[`step_${status}`]}`} aria-hidden>{glyph}</span>
      <span className={styles.stepLabel}>{label}</span>
      <span className={styles.stepState}>{status === 'queued' ? 'queued' : status === 'active' ? 'pending' : status === 'done' ? 'done' : 'failed'}</span>
    </li>
  );
}
