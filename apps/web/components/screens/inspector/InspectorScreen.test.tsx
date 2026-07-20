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

describe('InspectorScreen (REQ-019 / SEC-006/007 / AC-006/021)', () => {
  it('tells the 3-step story: agent proposed → law recomputed → score from evidence', () => {
    // II-W defect 6: the proposer step is producer-NEUTRAL ("Agent proposed") — the frontend has no
    // authoritative producer field, so it never claims LLM vs deterministic.
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/Agent proposed/i)).toBeInTheDocument();
    expect(screen.getByText(/Law recomputed/i)).toBeInTheDocument();
    expect(screen.getByText(/Score from evidence/i)).toBeInTheDocument();
  });

  it('renders the five JSON / data panels including the accent-bordered trusted recompute', () => {
    const { container } = render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText('MarketState')).toBeInTheDocument();
    expect(screen.getByText('AgentAction')).toBeInTheDocument();
    expect(screen.getByText(/Deterministic Recompute/i)).toBeInTheDocument();
    expect(screen.getByText(/CLV Explanation/i)).toBeInTheDocument();
    expect(container.querySelector('.accent')).toBeTruthy(); // trusted output accent border
  });

  it('fences the untrusted LLM metadata and labels it NOT AN INPUT TO SCORE (SEC-007)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/NOT AN INPUT TO SCORE/i)).toBeInTheDocument();
    expect(screen.getByText(/France controlling tempo/i)).toBeInTheDocument(); // rationale shown but fenced
  });

  it('marks the AgentAction panel params as untrusted (claimed_edge_bps must not read as authoritative)', () => {
    // II-W defect 6: worded producer-neutrally ("agent-supplied metadata") — deterministic strategies
    // emit reason/claimed_edge too, so it never asserts "LLM".
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/params include untrusted agent-supplied metadata/i)).toBeInTheDocument();
    expect(screen.getByText(/recorded, not scored/i)).toBeInTheDocument();
  });

  it('renders honest "—" (not 0.0%/0.000) for doctrine quantities absent from the proof artifact', () => {
    // A wire-sourced record (adaptInspector) has null doctrine quantities — these must
    // read as "not in proof artifact", never a plausible computed zero (no-overclaim).
    const gapRecord = {
      ...sampleInspectorRecord,
      clv_explanation: {
        fair_value_pct: null, closing_fair_value_pct: null, venue_decimal_price: null,
        mispricing_gap_bps: null, executable_edge_bps: null, real_venue_quote: false,
        clv_bps: 18.0, stake_fraction: null, plain: '',
      },
    };
    const clvSection = render(<InspectorScreen record={gapRecord} />).container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    expect(clvSection.textContent).not.toContain('0.0%');   // no fabricated fair-value/closing pct
    expect(clvSection.textContent).not.toContain('0.000');  // no fabricated venue price
    expect(clvSection.querySelectorAll('dd')[0].textContent).toBe('—'); // fair value absent
    expect(clvSection).toHaveTextContent('+18.0 bps'); // CLV (the real score) still renders
  });

  it('is read-only during a run with no editable affordances + shows READ-ONLY DURING RUN (AC-006)', () => {
    const { container } = render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/READ-ONLY DURING RUN/i)).toBeInTheDocument();
    // No data-entry elements (inputs/selects/textareas) — screen is fully read-only.
    expect(container.querySelectorAll('input, textarea, select').length).toBe(0);
    // InfoTip ⓘ triggers are informational-only buttons; no action/submit buttons.
    const nonInfoTipBtns = Array.from(container.querySelectorAll('button')).filter(
      (b) => !/^What is /.test(b.getAttribute('aria-label') ?? '')
    );
    expect(nonInfoTipBtns.length).toBe(0);
  });

  it('links to the full Proof Card for the run (AC-021)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByRole('link', { name: /View Full Proof Card/i })).toHaveAttribute('href', '/proof/run_7f3a');
  });

  // ---- Doctrine-quantities + InfoTip teeth (WD-5 Task 22) ----

  it('InfoTip copy is single-sourced from lib/glossary.ts — no per-screen microcopy drift (fair_value / executable_edge / clv / kelly)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    // The on-screen tooltip text MUST equal the glossary verbatim — no paraphrasing.
    expect(screen.getByText(GLOSSARY.fair_value.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.executable_edge.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.clv.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.kelly.definition)).toBeInTheDocument();
  });

  it('stake/Kelly is always "—" regardless of stake_fraction value (SEC-005 — never surfaced, not even in mock)', () => {
    // sampleInspectorRecord has stake_fraction: 0.06 — must still render "—", never "6.0%".
    const clvSection = render(<InspectorScreen record={sampleInspectorRecord} />)
      .container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    const dds = clvSection.querySelectorAll('dd');
    expect(dds[dds.length - 1].textContent).toBe('—'); // stake is the LAST row
    expect(clvSection.textContent).not.toContain('6.0%'); // no sizing value revealed
  });

  it('Executable Edge is a per-decision Inspector quantity — label renders here, not on Markets', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    // Scoped to the CLV section to avoid matching the clvPlain paragraph's lowercase "executable edge".
    const clvSection = screen.getByRole('region', { name: /CLV explanation/i });
    expect(clvSection).toHaveTextContent(/Executable Edge/i);
    // Edge value (+22.0 bps) renders inside the CLV explanation section.
    expect(clvSection).toHaveTextContent('+22.0 bps');
  });

  it('doctrine quantities populate under mock-populated fixture; five InfoTip triggers present (incl. Mispricing Gap)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    // Five InfoTip ⓘ triggers exist — Fair Value / Mispricing Gap / Executable Edge / CLV / Kelly.
    const triggers = screen.getAllByRole('button', { name: /^What is / });
    expect(triggers.length).toBe(5);
    // Fair Value and Executable Edge populate (non-"—") when clv_explanation carries values.
    const clvSection = screen.getByRole('region', { name: /CLV explanation/i });
    expect(clvSection.textContent).toContain('67.9%'); // fair_value_pct
    expect(clvSection.textContent).toContain('69.7%'); // closing_fair_value_pct
    expect(clvSection.textContent).toContain('1.472'); // venue_decimal_price
  });

  // ---- Edge DISPLAY GATE (REQ-2D-501 / AC-2D-501) — the presentation-layer honesty line ----

  it('DISPLAY GATE: a Fake/paper quote (fixed 2.05, real_venue_quote=false) NEVER surfaces as edge or gap', () => {
    // A venue price AND an edge number are present, but they are NOT from a real venue quote.
    // The gate must fail closed: no edge, no mispricing gap, no venue price rendered.
    const fakeRecord = {
      ...sampleInspectorRecord,
      clv_explanation: {
        ...sampleInspectorRecord.clv_explanation,
        venue_decimal_price: 2.05, mispricing_gap_bps: 512, executable_edge_bps: 512,
        real_venue_quote: false,
      },
    };
    const clvSection = render(<InspectorScreen record={fakeRecord} />)
      .container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    expect(clvSection.textContent).not.toContain('512');    // the Fake edge/gap number never appears
    expect(clvSection.textContent).not.toContain('2.050');  // the Fake venue price never appears
    // Executable Edge, Mispricing Gap, and venue price rows all read honest "—".
    const rows = Array.from(clvSection.querySelectorAll('dd')).map((d) => d.textContent);
    expect(rows.filter((t) => t === '—').length).toBeGreaterThanOrEqual(3);
  });

  it('REAL QUOTE: all four legibility quantities render — fair value, venue price, Mispricing Gap, Executable Edge — with GLOSSARY labels, gap labeled DISTINCTLY from edge', () => {
    const clvSection = render(<InspectorScreen record={sampleInspectorRecord} />)
      .container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    // The four render with a real quote (real_venue_quote=true):
    expect(clvSection.textContent).toContain('67.9%');   // TxLINE fair value
    expect(clvSection.textContent).toContain('1.472');   // venue decimal price
    expect(clvSection.textContent).toContain('+110.0 bps'); // mispricing gap (prob-space)
    expect(clvSection.textContent).toContain('+22.0 bps');  // executable edge (EV) — DISTINCT number
    // Labels are glossary-sourced AND distinct (mispricing_gap ≠ executable_edge).
    expect(clvSection.textContent).toContain(GLOSSARY.mispricing_gap.label);
    expect(clvSection.textContent).toContain(GLOSSARY.executable_edge.label);
    expect(GLOSSARY.mispricing_gap.label).not.toEqual(GLOSSARY.executable_edge.label);
    // The gap's InfoTip copy is the glossary verbatim (single-sourced, no per-screen drift).
    expect(screen.getByText(GLOSSARY.mispricing_gap.definition)).toBeInTheDocument();
  });

  it('shows the WD-7 low-sample confidence flag when clv_low_sample is set (shown, never hidden)', () => {
    const lowSample = {
      ...sampleInspectorRecord,
      clv_explanation: { ...sampleInspectorRecord.clv_explanation, clv_low_sample: true },
    };
    const clvSection = render(<InspectorScreen record={lowSample} />)
      .container.querySelector('[aria-label="CLV explanation"]') as HTMLElement;
    expect(clvSection.textContent).toMatch(/low sample/i);
  });
});
