'use client';
import { useEffect, useRef, useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { deriveMakerVerdict } from '@/lib/makerVerdict';
import { getAgentsRoster } from '@/lib/api';
import { useLane, type Lane } from '@/hooks/useLane';
import { useMakerArenaResult } from '@/hooks/useMakerArenaResult';
import type { PublicAgentRow } from '@/lib/catalog';
import type { MakerArenaResultView, MakerLeaderboardRow } from '@/lib/contracts';
import styles from './DuelScreen.module.css';

// A Public-Agents card renders ONLY factual PublicAgentRow fields — display name, archetype, mode,
// pooled Avg CLV, run count, valid %, proof state, safe public owner label, origin. Identity is
// public_agent_id only; operator_id/DID/owner_ref/instance_id are never exposed. Null perf → "—",
// never a fabricated 0. No proof-derived eligibility, no shared-evidence/anchored claim here.
function PublicCard({ agent, side }: { agent: PublicAgentRow; side: string }) {
  return (
    <div className={styles.card} data-testid="duel-card">
      <span className={styles.side}>{side}</span>
      <h2 className={styles.name}>{agent.display_name}</h2>
      <div className={styles.kv}><span>Archetype</span><span className="mono">{agent.archetype}</span></div>
      <div className={styles.kv}><span>Mode</span><span className="mono">{agent.mode ?? '—'}</span></div>
      <div className={styles.kv}><span>Pooled Avg CLV</span><span data-testid="duel-clv"><Num value={agent.avg_clv_bps} kind="bps" /></span></div>
      <div className={styles.kv}><span>Runs</span><span className="mono">{agent.runs ?? '—'}</span></div>
      <div className={styles.kv}><span>Valid %</span><span className="mono">{agent.valid_pct == null ? '—' : `${agent.valid_pct.toFixed(1)}%`}</span></div>
      <div className={styles.kv}><span>Proof</span><span data-testid="duel-proof"><Badge variant={agent.proof_state} /></span></div>
      <div className={styles.kv}><span>Owner</span><span className="mono">{agent.owner_public_label}</span></div>
      <div className={styles.kv}><span>Origin</span><span className="mono">{agent.origin}</span></div>
    </div>
  );
}

export function DuelScreen({
  // The mock gate is resolved by the PAGE in an effect (hydration-safe). Until it resolves,
  // `mockResolved` is false → a stable unresolved shell (honest-empty, NO fetch). Once resolved:
  // `mockAgents` non-null ⇒ MOCK path (use the injected rows, never the real reader); `mockAgents`
  // null ⇒ OFF-mock path (the SCREEN does the single real getAgentsRoster() read). No fixture here.
  mockResolved = false,
  mockAgents = null,
  // Maker result is page-sourced via useMakerArenaResult (F-9). No sealed-fixture default here —
  // an absent `makerResult` triggers the honest live-fetch/honest-empty maker path, never a fixture.
  makerResult,
}: {
  mockResolved?: boolean;
  mockAgents?: PublicAgentRow[] | null;
  makerResult?: MakerArenaResultView;
}) {
  // SINGLE lane authority (`resolved` gates the roster read until the URL lane is known) and SINGLE
  // real-fetch owner. Hooks run unconditionally, above every early return.
  const [lane, setLane, resolved] = useLane();
  const makerState = useMakerArenaResult(lane === 'maker', makerResult);

  // OFF-mock real read (E3's getAgentsRoster) — fired at most ONCE, only when the mock gate resolved
  // to "no injected rows", the URL lane resolved, AND the lane is the Public-Agents lane. The Maker
  // lane NEVER reads the roster; a direct `?lane=maker` load resolves to maker before this can fire.
  const [rosterRows, setRosterRows] = useState<PublicAgentRow[] | null>(null);
  const fetchStartedRef = useRef(false);
  // Cancellation is scoped to UNMOUNT only — never to a lane change. A `lane`-dependent cleanup would
  // discard an in-flight read the instant the user toggles to Maker, and the once-guard would then
  // block any retry, stranding Public Agents permanently empty (M5). This ref flips false solely on
  // unmount, so a mid-flight lane toggle can never drop the single result.
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);
  useEffect(() => {
    if (!mockResolved) return;           // unresolved shell: no fetch
    if (mockAgents) return;              // mock path: injected rows, no reader
    if (!resolved) return;              // wait for the URL lane before deciding to read
    if (lane !== 'directional') return; // Maker lane never reads the roster
    if (fetchStartedRef.current) return; // fire exactly once (per successful/in-flight read)
    fetchStartedRef.current = true;
    // No lane-scoped cleanup: the read COMPLETES across any lane toggle; only unmount ignores it.
    getAgentsRoster().then(
      (rows) => { if (mountedRef.current) setRosterRows(rows); },
      () => {
        // A genuine REJECT (getAgentsRoster normally catches → resolves []) may retry on next toggle:
        // release the once-guard so a later lane change can re-attempt, but never loop (deps unchanged).
        if (mountedRef.current) setRosterRows([]);
        fetchStartedRef.current = false;
      },
    );
  }, [mockResolved, mockAgents, resolved, lane]);

  // The Public-Agents rows: injected mock rows, else the off-mock roster read (null until it lands).
  const agents: PublicAgentRow[] = mockAgents ?? rosterRows ?? [];
  // Identity/selectors key on public_agent_id ONLY; duplicates collapse to one distinct option.
  const byId = new Map<string, PublicAgentRow>();
  for (const ag of agents) if (!byId.has(ag.public_agent_id)) byId.set(ag.public_agent_id, ag);
  const distinctIds = [...byId.keys()];
  const idsKey = distinctIds.join('|');

  const [aId, setAId] = useState('');
  const [bId, setBId] = useState('');
  // Reconcile the two selections whenever the DISTINCT roster changes: keep a still-valid selection,
  // replace a removed one, default to the first two DISTINCT ids, and never let both sides be equal.
  useEffect(() => {
    if (distinctIds.length < 2) return;
    const a = distinctIds.includes(aId) ? aId : distinctIds[0];
    const b = distinctIds.includes(bId) && bId !== a ? bId : (distinctIds.find((id) => id !== a) as string);
    if (a !== aId) setAId(a);
    if (b !== bId) setBId(b);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey]);

  // LANE SWITCH — a level above the per-agent compare (a different measurement). The visible option is
  // "Public Agents" (the internal enum value stays 'directional'); the note never claims "Directional".
  const laneSwitch = (
    <div className={styles.laneRow}>
      <span className={styles.laneLabel}>LANE</span>
      <SegmentedControl<Lane>
        ariaLabel="Duel lane"
        value={lane}
        onChange={setLane}
        options={[{ value: 'directional', label: 'Public Agents' }, { value: 'maker', label: 'Maker' }]}
      />
      <span className={styles.laneNote}>
        <b>Public Agents</b> compares two agents on <b>Avg CLV</b>; Maker runs the fixed <b>toxicity falsification</b>. Separate lanes, different agents.
      </span>
    </div>
  );

  if (lane === 'maker') {
    return (
      <section className={styles.screen} aria-label="Head-to-Head Duel">
        <h1 className={styles.title}>Head-to-Head Duel</h1>
        {laneSwitch}
        {makerState.status === 'loading' || makerState.status === 'idle' ? (
          <p className={styles.empty} data-testid="maker-loading" aria-live="polite">Loading maker result…</p>
        ) : makerState.status === 'unavailable' ? (
          <p className={styles.empty} data-testid="maker-unavailable" role="alert">Maker data unavailable.</p>
        ) : makerState.result.leaderboard.length < 2 ? (
          <p className={styles.empty} data-testid="maker-empty">Maker comparison unavailable: the result does not contain both fixed maker agents.</p>
        ) : (
          <MakerDuel result={makerState.result} />
        )}
      </section>
    );
  }

  // The Public-Agents compare needs ≥2 DISTINCT public agents. The unresolved shell (mock gate not yet
  // resolved) and the resolved-but-insufficient state are both honest-empty — never a fabricated pair.
  if (!mockResolved || distinctIds.length < 2) {
    return (
      <section className={styles.screen} aria-label="Public Agents · Summary Comparison">
        <h1 className={styles.title}>Public Agents · Summary Comparison</h1>
        {laneSwitch}
        <p className={styles.empty} data-testid="duel-empty">Select at least two public agents to compare.</p>
      </section>
    );
  }

  const a = byId.get(distinctIds.includes(aId) ? aId : distinctIds[0])!;
  const b = byId.get(distinctIds.includes(bId) && bId !== a.public_agent_id ? bId : (distinctIds.find((id) => id !== a.public_agent_id) as string))!;
  // Pooled Avg CLV difference — shown ONLY when BOTH sides carry a numeric pooled Avg CLV. Either
  // null ⇒ "—" (never a fabricated gap). This is a factual summary difference, NOT a controlled
  // head-to-head, a shared-evidence intersection, a winner, or a causal claim.
  const difference = a.avg_clv_bps == null || b.avg_clv_bps == null ? '—' : (a.avg_clv_bps - b.avg_clv_bps).toFixed(1);

  // Both sides must stay DISTINCT: each select excludes the other side's current id, so the pair can
  // never collapse to the same agent, and the bump keeps a valid selection if the excluded set shifts.
  function pickA(id: string) {
    setAId(id);
    if (id === b.public_agent_id) setBId(distinctIds.find((x) => x !== id) as string);
  }
  function pickB(id: string) {
    setBId(id);
    if (id === a.public_agent_id) setAId(distinctIds.find((x) => x !== id) as string);
  }

  return (
    <section className={styles.screen} aria-label="Public Agents · Summary Comparison">
      <h1 className={styles.title}>Public Agents · Summary Comparison</h1>
      {laneSwitch}

      <div className={styles.selectors}>
        <label className={styles.sel}><span className={styles.label}>Agent A</span>
          <select aria-label="Agent A" value={a.public_agent_id} onChange={(e) => pickA(e.target.value)} className={styles.select}>
            {distinctIds.filter((id) => id !== b.public_agent_id).map((id) => <option key={id} value={id}>{byId.get(id)!.display_name}</option>)}
          </select>
        </label>
        <span className={styles.vs}>vs</span>
        <label className={styles.sel}><span className={styles.label}>Agent B</span>
          <select aria-label="Agent B" value={b.public_agent_id} onChange={(e) => pickB(e.target.value)} className={styles.select}>
            {distinctIds.filter((id) => id !== a.public_agent_id).map((id) => <option key={id} value={id}>{byId.get(id)!.display_name}</option>)}
          </select>
        </label>
      </div>

      <div className={styles.cards}>
        <PublicCard agent={a} side="A" />
        <PublicCard agent={b} side="B" />
      </div>

      <p className={styles.divergence}>Pooled Avg CLV difference: <span className="mono">{difference} bps</span>. Each side is a public agent&apos;s own pooled Avg CLV across its runs — a factual summary, not a ranked contest.</p>
    </section>
  );
}

// Maker Arena lane (MM-R1) — the FIXED naive-mm vs txline-fair-mm pairing (no picker; only one
// maker pairing exists, SEC-005: a separate render path + data source). Headline metric is
// toxicity loss; DUEL RESULT is the three-state SEPARATED/INCONCLUSIVE/INVERTED falsification
// verdict + Δ + CI (derived via deriveMakerVerdict — never a boolean shortcut, I-R M1), never
// a CLV point-delta. The per-quote panel is honest-empty — no fabricated per-tick quote pairs.
function MakerDuel({ result }: { result: MakerArenaResultView }) {
  const ranked = [...result.leaderboard].sort((a, b) => a.avg_toxicity_loss_bps - b.avg_toxicity_loss_bps);
  const candidate = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'candidate') ?? ranked[0];
  const control = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'control') ?? ranked[1];
  const verdict = deriveMakerVerdict(result.falsification, {
    candidate: candidate.agent_id, control: control.agent_id,
  });
  const f = result.falsification;

  function MakerCard({ row, kind }: { row: MakerLeaderboardRow; kind: 'candidate' | 'control' }) {
    const meta = MAKER_AGENT_META[row.agent_id];
    // Winner treatment derives from the REAL verdict (I-R M1): the candidate only when
    // SEPARATED, the control only when INVERTED, nobody when INCONCLUSIVE/unknown.
    const isLessToxic = verdict.winner === kind;
    return (
      <div className={`${styles.card} ${isLessToxic ? styles.cardWinner : ''}`} data-testid="duel-maker-card">
        <div className={styles.makerCardHead}>
          <span className={styles.name}>{row.agent_id}</span>
          <Badge variant="mm-r1">{kind === 'candidate' ? 'CANDIDATE' : 'CONTROL'}</Badge>
          {isLessToxic ? <Badge variant={verdict.badge}>LESS TOXIC</Badge> : null}
        </div>
        <span className={styles.side}>{meta?.caption ?? kind} · MM-R1</span>
        <div className={styles.kv}><span>Toxicity loss ↓</span><span data-testid="duel-maker-toxicity"><Num value={row.avg_toxicity_loss_bps} kind="bps" /></span></div>
        <div className={styles.kv}><span>Markout <span className={styles.diagnosticTag}>ⓘ diagnostic</span></span><span className="mono">{row.avg_markout_bps}</span></div>
        <div className={styles.kv}><span>Quotes</span><span className="mono">{row.quote_count.toLocaleString()}</span></div>
        {/* Per-agent scored count — row.scored, NEVER the fixture-universe size (I-R M4). */}
        <div className={styles.kv}><span>Abstained · Scored</span><span className="mono" data-testid="duel-maker-scored">{row.abstained} · {row.scored.toLocaleString()}</span></div>
        <div className={styles.kv}><span>Exec edge</span><span className="mono" data-testid="duel-maker-edge">{String(row.real_executable_edge_bps)}</span></div>
        <div className={styles.kv}><span>Rung</span><Badge variant="mm-r1" /></div>
      </div>
    );
  }

  return (
    <>
      {/* A sealed tape is replay/verify identity, NOT external anchoring — the maker result
          carries no authoritative anchor_status, so this renders the honest not-anchored
          state and never a clickable/implied external-anchor claim (I-R M2). */}
      <div className={styles.evidence} data-testid="evidence-hash">
        <Badge variant="not-anchored" />
        <span className={`mono ${styles.evidenceText}`}>
          Fixed falsification pairing on the sealed maker tape · {result.fixture_universe_n} fixtures · n={result.fixture_universe_n}. Sealed tape identity only — no external anchor for this result. Only one maker pairing exists — no agent picker.
        </span>
        <Badge variant="small-n">n={result.fixture_universe_n} · small sample</Badge>
      </div>

      <div className={styles.cards}>
        <MakerCard row={candidate} kind="candidate" />
        <MakerCard row={control} kind="control" />
      </div>

      <div className={styles.duelResult} data-testid="duel-result" data-verdict={verdict.kind.toLowerCase()}>
        <Badge variant={verdict.badge}>{verdict.badgeText}</Badge>
        <div className={styles.falsificationBody}>
          <div className={styles.falsificationHeadline}>{verdict.headline}</div>
          <div className={styles.falsificationSub}>{verdict.ciSub}</div>
        </div>
        <div className={styles.falsificationStats}>
          <div className={styles.stat}><span className={styles.statLabel}>Δ QUOTE QUALITY</span><Num value={f.delta_bps} kind="bps" /></div>
          <div className={styles.stat}><span className={styles.statLabel}>95% CI</span><span className="mono">[{f.ci_low_bps}, {f.ci_high_bps}]</span></div>
        </div>
      </div>
      <p className={styles.divergence}>The CI is the point — a maker duel asks &quot;is the difference real?&quot;, not &quot;who scored more&quot;. A less-toxic side is only crowned when the CI excludes zero (SEPARATED, or INVERTED for the control); an INCONCLUSIVE result shows no winner.</p>

      <div className={styles.card}>
        <span className={styles.side}>PER-QUOTE DIVERGENCE</span>
        <p className={styles.empty} data-testid="duel-per-quote-empty">Per-quote divergence not yet surfaced by the API (future). The maker result carries aggregates only — no fabricated per-tick quote pairs.</p>
      </div>

      <p className={styles.footNoteBlock}>
        Maker markout is diagnostic geometry only, never the axis — the control&apos;s higher markout is more toxic. Makers hold no positions, so there is no valid % / drawdown / PnL. <span className="mono">real_executable_edge_bps</span> is null by construction.
      </p>
    </>
  );
}
