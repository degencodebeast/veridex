'use client';
import { useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { isEligible } from '@/lib/derive';
import { AGENTS } from '@/lib/fixtures/catalog';
import type { AgentSummary } from '@/lib/catalog';
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

export function DuelScreen({ agents = AGENTS }: { agents?: AgentSummary[] }) {
  // Hooks run unconditionally; default ids safely even when <2 agents are supplied.
  const [aId, setAId] = useState(agents[0]?.agent_id ?? '');
  const [bId, setBId] = useState(agents[1]?.agent_id ?? '');

  // A duel needs two agents — render an honest empty state rather than crashing on agents[1].
  if (agents.length < 2) {
    return (
      <section className={styles.screen} aria-label="Head-to-Head Duel">
        <h1 className={styles.title}>Head-to-Head Duel</h1>
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
