import { describe, it, expect } from 'vitest';
import { agentSummaryToPublicRow } from '@/lib/agent-roster';
import type { AgentSummary } from '@/lib/catalog';

// A SCORED mock agent (numeric avg_clv_bps) → proof_state = the real proof_mode; perf carried verbatim.
const scored: AgentSummary = {
  agent_id: 'value_clv', agent_name: 'Value CLV', archetype: 'value_clv', mode: 'numeric',
  avg_clv_bps: 18.4, runs: 14, proof_mode: 'reproducible', source_mode: 'live', valid_pct: 95.0, source: 'STUDIO',
};

// An UNSCORED mock agent (avg_clv_bps == null) → proof_state = 'unscored'; perf stays null (honest "—").
const unscored: AgentSummary = {
  agent_id: 'fresh', agent_name: 'Fresh Agent', archetype: 'baseline', mode: null,
  avg_clv_bps: null, runs: null, proof_mode: 'partial', source_mode: 'replay', valid_pct: null, source: 'BYOA',
};

describe('agentSummaryToPublicRow (the SHARED adapter — E4 Duel consumes this ONE mapping)', () => {
  it('maps a scored AgentSummary → proof_state = proof_mode, numeric perf carried, identity mapped', () => {
    const row = agentSummaryToPublicRow(scored);
    expect(row.public_agent_id).toBe('value_clv'); // == agent_id
    expect(row.display_name).toBe('Value CLV');     // == agent_name
    expect(row.owner_public_label).toBe('demo');
    expect(row.origin).toBe('unknown');
    expect(row.proof_state).toBe('reproducible');   // scored ⇒ the real proof_mode
    expect(row.archetype).toBe('value_clv');
    expect(row.mode).toBe('numeric');
    expect(row.avg_clv_bps).toBe(18.4);
    expect(row.runs).toBe(14);
    expect(row.valid_pct).toBe(95.0);
  });

  it('maps an unscored AgentSummary (avg_clv_bps == null) → proof_state = "unscored", perf null', () => {
    const row = agentSummaryToPublicRow(unscored);
    expect(row.proof_state).toBe('unscored');       // null CLV ⇒ honest unscored, never the proof_mode
    expect(row.avg_clv_bps).toBeNull();
    expect(row.runs).toBeNull();
    expect(row.valid_pct).toBeNull();
    expect(row.mode).toBeNull();
    expect(row.public_agent_id).toBe('fresh');
    expect(row.display_name).toBe('Fresh Agent');
    expect(row.owner_public_label).toBe('demo');
    expect(row.origin).toBe('unknown');
    expect(row.archetype).toBe('baseline');
  });
});
