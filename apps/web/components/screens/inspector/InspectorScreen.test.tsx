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
  it('tells the 3-step story: LLM proposed → law recomputed → score from evidence', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/LLM proposed/i)).toBeInTheDocument();
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
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/params include untrusted LLM claims/i)).toBeInTheDocument();
    expect(screen.getByText(/recorded, not scored/i)).toBeInTheDocument();
  });

  it('renders honest "—" (not 0.0%/0.000) for doctrine quantities absent from the proof artifact', () => {
    // A wire-sourced record (adaptInspector) has null doctrine quantities — these must
    // read as "not in proof artifact", never a plausible computed zero (no-overclaim).
    const gapRecord = {
      ...sampleInspectorRecord,
      clv_explanation: {
        fair_value_pct: null, closing_fair_value_pct: null, venue_decimal_price: null,
        executable_edge_bps: null, clv_bps: 18.0, stake_fraction: null, plain: '',
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
    expect(dds[3].textContent).toBe('—'); // stake row is 4th dd
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

  it('doctrine quantities populate under mock-populated fixture; all four InfoTip triggers present', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    // Four InfoTip ⓘ triggers exist — one per doctrine quantity label.
    const triggers = screen.getAllByRole('button', { name: /^What is / });
    expect(triggers.length).toBe(4);
    // Fair Value and Executable Edge populate (non-"—") when clv_explanation carries values.
    const clvSection = screen.getByRole('region', { name: /CLV explanation/i });
    expect(clvSection.textContent).toContain('67.9%'); // fair_value_pct
    expect(clvSection.textContent).toContain('69.7%'); // closing_fair_value_pct
    expect(clvSection.textContent).toContain('1.472'); // venue_decimal_price
  });
});
