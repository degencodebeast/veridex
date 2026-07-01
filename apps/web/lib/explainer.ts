// Proof Explainer (Phase B) — the READ-ONLY educational box. It consumes ONLY the /explain response
// envelope; it NEVER verifies/certifies. The disclaimer + footer mirror the backend verbatim
// (veridex/explainer/proof_explainer.py) so the box carries them even on the client-only template path.

export const EXPLAINER_DISCLAIMER =
  'Explainer (LLM) · educational only · does not verify, score, certify, or control agents. '
  + 'The deterministic verifier is the source of truth.';

export const EXPLAINER_FOOTER = 'Source of truth: deterministic Verify result + Proof Card fields.';

export interface ProofExplanation {
  explanation: string;
  disclaimer: string;
  footer: string;
}

// ⚑ Client-side validity-intent detector (load-bearing): a validity/pass/certify question is
// short-circuited to the FIXED template and NEVER reaches the LLM/endpoint. The LLM never answers
// "is this run valid?" freeform.
const VALIDITY_RE =
  /\b(valid|invalid|verif(y|ied|ies)?|pass(ed|es)?|legit(imate)?|certif(y|ied|ication)?|prove[dn]?|proof is (ok|good|right)|correct|trust(ed|worthy)?|genuine|authentic|is (this|it) (run )?(ok|right|good|real))\b/i;

export function isValidityQuestion(q: string): boolean {
  return VALIDITY_RE.test(q);
}

// The FIXED, NON-LLM answer for a validity question — cites the deterministic Verify result and
// routes the user to the Proof Checks. NEVER an LLM freeform validity claim.
export function validityTemplate(verified: boolean | null): string {
  const state = verified === true
    ? 'verified'
    : verified === false
      ? 'NOT verified'
      : 'not yet run — click Verify to see the deterministic result';
  return `I cannot verify or certify runs. The deterministic Verify result says: ${state}. See the Proof Checks.`;
}

// DEMO narration shown under mock mode (no backend). Clearly labelled DEMO; never claims to verify;
// carries the same disclaimer + footer as a live response.
export const PROOF_EXPLAIN_DEMO: ProofExplanation = {
  explanation:
    '[DEMO] This proof card was produced by the deterministic verifier: the 7 checks certify the '
    + 'record, the metrics (CLV etc.) rank performance, and the anchor commits the manifest hash to '
    + 'Solana. This narration is educational only — it does not verify or score anything.',
  disclaimer: EXPLAINER_DISCLAIMER,
  footer: EXPLAINER_FOOTER,
};
