import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { sampleInspectorRecord } from '@/__tests__/fixtures/contracts';
import { GLOSSARY } from '@/lib/glossary';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

// II-W defect 2 · A CLV in the PENDING state (backend emits the non-numeric "pending" sentinel for a
// valid WAIT/abstention — veridex/api/router.py:796 `clv_bps = score_row.get("clv_bps", "pending")`;
// InspectorRecord.clv_bps is typed `int | str` — veridex/api/schemas.py:290) must render a PENDING
// affordance, NEVER a fabricated `0 bps`. This is DISTINCT from the F-5 / R-globalclv null/unscored
// CLV → "—" fix: pending is "too little runway to score yet" (GLOSSARY.clv_pending), an honest
// abstention, not an absent value. The pre-fix adapter coerced "pending" via `Number('pending') || 0`
// → a fabricated 0.
describe('II-W defect 2 · Inspector renders PENDING CLV honestly, never a fabricated 0 bps', () => {
  it('a pending-CLV record shows the PENDING affordance and NO 0-bps score number', () => {
    const pending = {
      ...sampleInspectorRecord,
      clv_explanation: {
        ...sampleInspectorRecord.clv_explanation,
        clv_bps: 0, clv_pending: true,
      },
    };
    const clvSection = render(<InspectorScreen record={pending} />)
      .container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    // The SCORE chip renders the honest PENDING affordance (single-sourced from the glossary), NEVER a
    // fabricated bps number — the pre-fix code rendered fmtBps(0) = "SCORE = +0.0 bps" here.
    const scoreChip = clvSection.querySelector('[class*="scoreChip"]') as HTMLElement;
    expect(scoreChip.textContent).toBe(`SCORE = ${GLOSSARY.clv_pending.label}`);
    expect(scoreChip.textContent).not.toMatch(/bps/);
    // The CLV quantity cell (last-but-one dd — CLV sits above the always-"—" stake row) shows pending too.
    const dds = Array.from(clvSection.querySelectorAll('dd')).map((d) => d.textContent);
    expect(dds).toContain(GLOSSARY.clv_pending.label);
  });
});

// II-W defect 6 · The Inspector's decision story must DISTINGUISH a deterministic agent's proposal
// from an LLM's — it must NOT always say "LLM proposed". The backend marks this by the presence of
// `untrusted_llm_metadata` (reason/confidence/claimed_edge_bps): POPULATED for an LLM action, EMPTY
// {} for a deterministic agent that emits none (veridex/api/router.py:803-805). The view-model
// carries this as `untrusted_llm` (null ⇒ deterministic). Labeling a deterministic action "LLM
// proposed" fabricates an LLM in the provenance story.
describe('II-W defect 6 · Inspector distinguishes deterministic vs LLM proposer', () => {
  it('a deterministic action (untrusted_llm=null) is NOT labeled "LLM proposed"', () => {
    const deterministic = { ...sampleInspectorRecord, untrusted_llm: null };
    render(<InspectorScreen record={deterministic} />);
    expect(screen.queryByText(/LLM proposed/i)).toBeNull();
    expect(screen.getByText(/Deterministic proposal/i)).toBeInTheDocument();
  });

  it('an LLM action (untrusted_llm present) still reads "LLM proposed" (unchanged)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/LLM proposed/i)).toBeInTheDocument();
  });
});
