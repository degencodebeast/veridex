import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProofExplainer } from '@/components/screens/proof/ProofExplainer';
import { EXPLAINER_DISCLAIMER, EXPLAINER_FOOTER } from '@/lib/explainer';

// spy the reader so we can PROVE a validity question never reaches it (the load-bearing fence).
const explainProof = vi.fn();
vi.mock('@/lib/api', () => ({ explainProof: (...a: unknown[]) => explainProof(...a) }));

beforeEach(() => {
  explainProof.mockReset();
  explainProof.mockResolvedValue({ explanation: 'LLM narration about CLV', disclaimer: EXPLAINER_DISCLAIMER, footer: EXPLAINER_FOOTER });
});
afterEach(() => { vi.unstubAllEnvs(); });

describe('ProofExplainer (Phase B — read-only, fenced)', () => {
  it('is named "Explain this Proof" and NEVER a banned name', () => {
    render(<ProofExplainer runId="r1" verified />);
    expect(screen.getByRole('heading', { name: /explain this proof/i })).toBeInTheDocument();
    expect(screen.queryByText(/proof chat|ai auditor|verify with ai/i)).toBeNull();
  });

  it('always shows the hard disclaimer', () => {
    render(<ProofExplainer runId="r1" verified />);
    expect(screen.getByTestId('explainer-disclaimer')).toHaveTextContent(EXPLAINER_DISCLAIMER);
  });

  it('⚑ a VALIDITY question → the FIXED template citing the deterministic Verify + Proof-Checks pointer, and NEVER calls the LLM', async () => {
    const user = userEvent.setup();
    render(<ProofExplainer runId="r1" verified />);
    await user.type(screen.getByTestId('explainer-input'), 'is this run valid?');
    await user.click(screen.getByRole('button', { name: /explain this proof/i }));
    const out = screen.getByTestId('explainer-result');
    expect(out).toHaveTextContent(/cannot verify or certify runs/i);
    expect(out).toHaveTextContent(/deterministic verify result says: verified/i);
    expect(out).toHaveTextContent(/see the proof checks/i);
    expect(screen.getByTestId('validity-route')).toBeInTheDocument();
    // THE load-bearing assertion: the LLM/endpoint was NEVER called for a validity question.
    expect(explainProof).not.toHaveBeenCalled();
  });

  it('a NON-validity question → calls the endpoint + renders the narration + footer', async () => {
    const user = userEvent.setup();
    render(<ProofExplainer runId="r1" verified />);
    await user.type(screen.getByTestId('explainer-input'), 'what does this field mean?');
    await user.click(screen.getByRole('button', { name: /explain this proof/i }));
    await waitFor(() => expect(explainProof).toHaveBeenCalledWith('r1', { question: 'what does this field mean?' }));
    expect(screen.getByTestId('explainer-result')).toHaveTextContent(/LLM narration about CLV/);
    expect(screen.getByTestId('explainer-footer')).toHaveTextContent(EXPLAINER_FOOTER);
  });

  it('graceful-unavailable: an "unavailable" envelope renders honestly, never fabricated', async () => {
    explainProof.mockResolvedValue({ explanation: 'Explainer unavailable (no LLM key configured).', disclaimer: EXPLAINER_DISCLAIMER, footer: EXPLAINER_FOOTER });
    const user = userEvent.setup();
    render(<ProofExplainer runId="r1" verified />);
    await user.type(screen.getByTestId('explainer-input'), 'what does this field mean?');
    await user.click(screen.getByRole('button', { name: /explain this proof/i }));
    await waitFor(() => expect(screen.getByTestId('explainer-result')).toHaveTextContent(/unavailable/i));
  });

  it('renders NO green check / verifier icon / valid badge of its own (defers validity to the deterministic block)', () => {
    render(<ProofExplainer runId="r1" verified />);
    const box = screen.getByTestId('proof-explainer');
    expect(within(box).queryByText(/✓|✔|valid ✓/)).toBeNull(); // no checkmark/badge of its own
  });

  it('is DEMO-labeled under mock mode', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<ProofExplainer runId="r1" verified />);
    expect(screen.getByTestId('explainer-demo')).toBeInTheDocument();
  });
});
