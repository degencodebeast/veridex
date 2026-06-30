import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { sampleInspectorRecord } from '@/__tests__/fixtures/contracts';

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

  it('is read-only during a run with no editable affordances + shows READ-ONLY DURING RUN (AC-006)', () => {
    const { container } = render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByText(/READ-ONLY DURING RUN/i)).toBeInTheDocument();
    expect(container.querySelectorAll('input, textarea, select, button').length).toBe(0);
  });

  it('links to the full Proof Card for the run (AC-021)', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    expect(screen.getByRole('link', { name: /View Full Proof Card/i })).toHaveAttribute('href', '/proof/run_7f3a');
  });
});
