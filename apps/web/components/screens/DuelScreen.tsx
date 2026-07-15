'use client';
import { useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { isEligible } from '@/lib/derive';
import { AGENTS } from '@/lib/fixtures/catalog';
import { MAKER_ARENA_RESULT, MAKER_AGENT_META } from '@/lib/fixtures/maker';
import { useLane, type Lane } from '@/hooks/useLane';
import type { AgentSummary } from '@/lib/catalog';
import type { MakerArenaResultView, MakerLeaderboardRow } from '@/lib/contracts';
import styles from './DuelScreen.module.css';

function DuelCard({ agent, side }: { agent: AgentSummary; side: string }) {
  return (
    <div className={styles.card} data-testid="duel-card">
      <span className={styles.side}>{side}</span>
      <h2 className={styles.name}>{agent.agent_name}</h2>
      <div className={styles.kv}><span>Avg CLV</span><span data-testid="duel-clv"><Num value={agent.avg_clv_bps} kind="bps" /></span></div>
      <div className={styles.kv}><span>Valid %</span><span className="mono">{agent.valid_pct.toFixed(1)}%</span></div>
      <div className={styles.kv}><span>Proof</span><span data-testid="duel-proof"><Badge variant={agent.proof_mode} /></span></div>
      <div className={styles.kv}><span>Eligibility</span><Badge variant={isEligible(agent.proof_mode) ? 'eligible' : 'not-eligible'} /></div>
    </div>
  );
}

export function DuelScreen({
  agents = AGENTS,
  makerResult = MAKER_ARENA_RESULT,
}: {
  agents?: AgentSummary[];
  makerResult?: MakerArenaResultView;
}) {
  const [lane, setLane] = useLane();
  // Hooks run unconditionally; default ids safely even when <2 agents are supplied.
  const [aId, setAId] = useState(agents[0]?.agent_id ?? '');
  const [bId, setBId] = useState(agents[1]?.agent_id ?? '');

  // LANE SWITCH — a level above the directional agent picker (a different measurement).
  const laneSwitch = (
    <div className={styles.laneRow}>
      <span className={styles.laneLabel}>LANE</span>
      <SegmentedControl<Lane>
        ariaLabel="Duel lane"
        value={lane}
        onChange={setLane}
        options={[{ value: 'directional', label: 'Directional' }, { value: 'maker', label: 'Maker' }]}
      />
      <span className={styles.laneNote}>
        Directional compares two agents on <b>Avg CLV</b>; Maker runs the fixed <b>toxicity falsification</b>. Separate lanes, different agents.
      </span>
    </div>
  );

  if (lane === 'maker') {
    return (
      <section className={styles.screen} aria-label="Head-to-Head Duel">
        <h1 className={styles.title}>Head-to-Head Duel</h1>
        {laneSwitch}
        <MakerDuel result={makerResult} />
      </section>
    );
  }

  // A directional duel needs two agents — render an honest empty state rather than crashing.
  if (agents.length < 2) {
    return (
      <section className={styles.screen} aria-label="Head-to-Head Duel">
        <h1 className={styles.title}>Head-to-Head Duel</h1>
        {laneSwitch}
        <p className={styles.empty} data-testid="duel-empty">Select at least two agents to run a head-to-head.</p>
      </section>
    );
  }

  const a = agents.find((x) => x.agent_id === aId) ?? agents[0];
  const b = agents.find((x) => x.agent_id === bId) ?? agents[1];
  const divergence = (a.avg_clv_bps - b.avg_clv_bps).toFixed(1);

  return (
    <section className={styles.screen} aria-label="Head-to-Head Duel">
      <h1 className={styles.title}>Head-to-Head Duel</h1>
      {laneSwitch}

      <div className={styles.evidence} data-testid="evidence-hash">
        <Badge variant="anchored" />
        <span className={`mono ${styles.evidenceText}`}>SAME SEALED EVIDENCE · evidence_hash 0xseal_fra_bra_8a31 · law recomputes each agent independently</span>
      </div>

      <div className={styles.selectors}>
        <label className={styles.sel}><span className={styles.label}>Agent A</span>
          <select aria-label="Agent A" value={aId} onChange={(e) => setAId(e.target.value)} className={styles.select}>
            {agents.map((x) => <option key={x.agent_id} value={x.agent_id}>{x.agent_name}</option>)}
          </select>
        </label>
        <span className={styles.vs}>vs</span>
        <label className={styles.sel}><span className={styles.label}>Agent B</span>
          <select aria-label="Agent B" value={bId} onChange={(e) => setBId(e.target.value)} className={styles.select}>
            {agents.map((x) => <option key={x.agent_id} value={x.agent_id}>{x.agent_name}</option>)}
          </select>
        </label>
      </div>

      <div className={styles.cards}>
        <DuelCard agent={a} side="A" />
        <DuelCard agent={b} side="B" />
      </div>

      <p className={styles.divergence}>Key divergence · Avg CLV gap <span className="mono">{divergence} bps</span> on identical sealed evidence.</p>
    </section>
  );
}

// Maker Arena lane (MM-R1) — the FIXED naive-mm vs txline-fair-mm pairing (no picker; only one
// maker pairing exists, SEC-005: a separate render path + data source). Headline metric is
// toxicity loss; DUEL RESULT is the SEPARATED/INCONCLUSIVE falsification verdict + Δ + CI, never
// a CLV point-delta. The per-quote panel is honest-empty — no fabricated per-tick quote pairs.
function MakerDuel({ result }: { result: MakerArenaResultView }) {
  const ranked = [...result.leaderboard].sort((a, b) => a.avg_toxicity_loss_bps - b.avg_toxicity_loss_bps);
  const candidate = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'candidate') ?? ranked[0];
  const control = ranked.find((r) => MAKER_AGENT_META[r.agent_id]?.role === 'control') ?? ranked[1];
  const f = result.falsification;
  const separated = f.verdict === 'SEPARATED';

  function MakerCard({ row, kind }: { row: MakerLeaderboardRow; kind: 'candidate' | 'control' }) {
    const meta = MAKER_AGENT_META[row.agent_id];
    return (
      <div className={`${styles.card} ${kind === 'candidate' ? styles.cardWinner : ''}`} data-testid="duel-maker-card">
        <div className={styles.makerCardHead}>
          <span className={styles.name}>{row.agent_id}</span>
          <Badge variant={kind === 'candidate' ? 'separated' : 'mm-r1'}>{kind === 'candidate' ? 'LESS TOXIC' : 'CONTROL'}</Badge>
        </div>
        <span className={styles.side}>{meta?.caption ?? kind} · MM-R1</span>
        <div className={styles.kv}><span>Toxicity loss ↓</span><span data-testid="duel-maker-toxicity"><Num value={row.avg_toxicity_loss_bps} kind="bps" /></span></div>
        <div className={styles.kv}><span>Markout <span className={styles.diagnosticTag}>ⓘ diagnostic</span></span><span className="mono">{row.avg_markout_bps}</span></div>
        <div className={styles.kv}><span>Quotes</span><span className="mono">{row.quote_count.toLocaleString()}</span></div>
        <div className={styles.kv}><span>Abstained · Scored</span><span className="mono">{row.abstained} · {result.fixture_universe_n}</span></div>
        <div className={styles.kv}><span>Exec edge</span><span className="mono" data-testid="duel-maker-edge">{String(row.real_executable_edge_bps)}</span></div>
        <div className={styles.kv}><span>Rung</span><Badge variant="mm-r1" /></div>
      </div>
    );
  }

  return (
    <>
      <div className={styles.evidence} data-testid="evidence-hash">
        <Badge variant="anchored" />
        <span className={`mono ${styles.evidenceText}`}>
          Fixed falsification pairing on the sealed maker tape · {result.fixture_universe_n} fixtures · n={result.fixture_universe_n}. Only one maker pairing exists — no agent picker.
        </span>
        <Badge variant="small-n">n={result.fixture_universe_n} · small sample</Badge>
      </div>

      <div className={styles.cards}>
        <MakerCard row={candidate} kind="candidate" />
        <MakerCard row={control} kind="control" />
      </div>

      <div className={styles.duelResult} data-testid="duel-result">
        <Badge variant={separated ? 'separated' : 'inconclusive'} />
        <div className={styles.falsificationBody}>
          <div className={styles.falsificationHeadline}>
            {separated
              ? 'txline-fair-mm is less toxic than the naive control'
              : 'No separation between txline-fair-mm and the naive control'}
          </div>
          <div className={styles.falsificationSub}>
            {separated ? 'whole 95% CI above zero → difference is real' : 'CI spans zero → difference is not distinguishable from noise'}
          </div>
        </div>
        <div className={styles.falsificationStats}>
          <div className={styles.stat}><span className={styles.statLabel}>Δ QUOTE QUALITY</span><Num value={f.delta_bps} kind="bps" /></div>
          <div className={styles.stat}><span className={styles.statLabel}>95% CI</span><span className="mono">[{f.ci_low_bps}, {f.ci_high_bps}]</span></div>
        </div>
      </div>
      <p className={styles.divergence}>The CI is the point — a maker duel asks &quot;is the difference real?&quot;, not &quot;who scored more&quot;. A winner is only crowned when the verdict is SEPARATED; an INCONCLUSIVE result shows no winner.</p>

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
