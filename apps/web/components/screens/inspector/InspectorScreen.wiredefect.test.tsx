import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { adaptInspector } from '@/lib/api';
import { GLOSSARY } from '@/lib/glossary';
import type * as W from '@/lib/wire';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

// BACKEND-SHAPED fixtures: a real wire-shaped InspectorRecord routed THROUGH adaptInspector, then
// rendered — so each RED proves the backend value reaches the screen, not a hand-built view model.
function wireInspector(over: Partial<W.InspectorRecord>): W.InspectorRecord {
  return {
    run_id: 'run_7f3a', agent_id: 'agt_x', tick_seq: 5,
    market_state: {}, agent_action: { type: 'FLAG_VALUE', params: {} },
    recompute: { recomputed_edge_bps: 22, clv_bps: 18, valid: true },
    clv_bps: 18, untrusted_llm_metadata: {}, ...over,
  } as W.InspectorRecord;
}

// II-W defect 2 (Minor fold) · PENDING must reach the screen THROUGH adaptInspector — the backend
// "pending" sentinel (router.py:796; clv_bps: int|str, schemas.py:290) drives BOTH the headline
// PENDING affordance and the recompute echo's honest null, never a fabricated 0.
describe('II-W defect 2 · a pending wire clv_bps renders PENDING end-to-end (adapter → screen)', () => {
  it('headline SCORE chip shows PENDING and the recompute echo shows null (never a 0)', () => {
    const rec = adaptInspector(wireInspector({
      clv_bps: 'pending', recompute: { recomputed_edge_bps: 0, clv_bps: 'pending', valid: true },
    }));
    const { container } = render(<InspectorScreen record={rec} />);
    const scoreChip = container.querySelector('[class*="scoreChip"]') as HTMLElement;
    expect(scoreChip.textContent).toBe(`SCORE = ${GLOSSARY.clv_pending.label}`);
    expect(scoreChip.textContent).not.toMatch(/bps/);
    // the Deterministic Recompute JSON echo preserves the sentinel as null, never a fabricated 0
    const recomputePanel = screen.getByText('Deterministic Recompute').parentElement as HTMLElement;
    expect(recomputePanel.textContent).toMatch(/"clv_bps"\s*:\s*null/);
    expect(recomputePanel.textContent).not.toMatch(/"clv_bps"\s*:\s*0/);
  });
});

// II-W defect 6 (REWORKED per Codex) · The frontend has NO authoritative producer field. The backend
// copies reason/confidence/claimed_edge from GENERIC action params (router.py:803-805); DETERMINISTIC
// strategies emit that metadata (veridex/strategies/drift.py:180-183) while an LLM WAIT carries EMPTY
// params (veridex/strategies/llm_drift.py:147-149). So metadata presence does NOT indicate producer —
// inferring kind is unreliable/inverted. The honest resolution is a NEUTRAL label ("Agent proposed").
// A real det-vs-LLM distinction needs a backend proposer_kind field (out of scope — follow-up).
describe('II-W defect 6 · Inspector uses a NEUTRAL proposer label — never infers agent kind from metadata', () => {
  it('a DETERMINISTIC action carrying reason/claimed_edge metadata is NOT labeled "LLM proposed"', () => {
    // drift.py emits reason + claimed_edge_bps on FOLLOW_MOMENTUM — a deterministic producer.
    const rec = adaptInspector(wireInspector({
      agent_action: { type: 'FOLLOW_MOMENTUM', params: { market_key: 'm', side: 'FRA', reason: 'cumulative drift', claimed_edge_bps: 42 } },
      untrusted_llm_metadata: { reason: 'cumulative drift', claimed_edge_bps: 42 },
    }));
    render(<InspectorScreen record={rec} />);
    expect(screen.queryByText(/LLM proposed/i)).toBeNull();
    expect(screen.getByText(/Agent proposed/i)).toBeInTheDocument();
  });

  it('an LLM WAIT with EMPTY params is NOT labeled "Deterministic"', () => {
    // llm_drift.py _wait() returns params={} — an LLM producer with no metadata.
    const rec = adaptInspector(wireInspector({
      agent_action: { type: 'WAIT', params: {} }, untrusted_llm_metadata: {},
    }));
    render(<InspectorScreen record={rec} />);
    expect(screen.queryByText(/Deterministic proposal/i)).toBeNull();
    expect(screen.getByText(/Agent proposed/i)).toBeInTheDocument();
  });

  it('the untrusted-metadata note renders ONLY when metadata is present, and is worded agent-supplied (not LLM)', () => {
    const withMeta = adaptInspector(wireInspector({ untrusted_llm_metadata: { reason: 'r', claimed_edge_bps: 5 } }));
    const withNone = adaptInspector(wireInspector({ untrusted_llm_metadata: {} }));
    const a = render(<InspectorScreen record={withMeta} />);
    // scope to the AgentAction NOTE text (the section header also carries "agent-supplied metadata")
    expect(a.getByText(/params include untrusted agent-supplied metadata/i)).toBeInTheDocument();
    expect(a.queryByText(/untrusted LLM claims/i)).toBeNull();
    a.unmount();
    const b = render(<InspectorScreen record={withNone} />);
    // no agent-supplied metadata ⇒ the note is absent entirely (never printed unconditionally)
    expect(b.queryByText(/params include untrusted agent-supplied metadata/i)).toBeNull();
  });
});
