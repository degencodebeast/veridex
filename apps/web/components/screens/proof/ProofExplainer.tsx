'use client';
import { useEffect, useState, type FormEvent } from 'react';
import { explainProof } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import {
  EXPLAINER_DISCLAIMER, EXPLAINER_FOOTER, isValidityQuestion, validityTemplate,
  type ProofExplanation,
} from '@/lib/explainer';
import styles from './ProofExplainer.module.css';

// "Explain this Proof" — READ-ONLY educational box. It consumes ONLY the /explain response envelope
// and NEVER verifies/certifies. Validity/pass questions are short-circuited CLIENT-SIDE to a fixed,
// non-LLM template that cites the deterministic Verify result + routes to the Proof Checks — the LLM
// is never called for "is this valid?". No green check / verifier icon / valid badge of its own.
export function ProofExplainer({ runId, verified }: { runId: string; verified: boolean | null }) {
  const [question, setQuestion] = useState('');
  const [result, setResult] = useState<ProofExplanation | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('idle');
  const [validityRoute, setValidityRoute] = useState(false);
  const [mock, setMock] = useState(false);
  useEffect(() => { setMock(isMockEnabled()); }, []);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    if (isValidityQuestion(q)) {
      // ⚑ LOAD-BEARING: a validity question NEVER reaches the LLM/endpoint — fixed deterministic answer.
      setValidityRoute(true);
      setResult({ explanation: validityTemplate(verified), disclaimer: EXPLAINER_DISCLAIMER, footer: EXPLAINER_FOOTER });
      setStatus('idle');
      return;
    }
    setValidityRoute(false);
    setStatus('loading');
    try {
      setResult(await explainProof(runId, { question: q }));
      setStatus('idle');
    } catch {
      setResult(null);
      setStatus('error');
    }
  };

  return (
    <section className={styles.box} aria-label="Explain this Proof" data-testid="proof-explainer">
      <div className={styles.head}>
        <h3 className={styles.title}>Explain this Proof</h3>
        {mock && <span className={styles.demo} data-testid="explainer-demo">DEMO</span>}
      </div>
      {/* Hard label — ALWAYS visible, never dressed as a verifier. */}
      <p className={styles.disclaimer} data-testid="explainer-disclaimer">{EXPLAINER_DISCLAIMER}</p>
      <form className={styles.form} onSubmit={onSubmit}>
        <input
          className={styles.input}
          data-testid="explainer-input"
          aria-label="Ask what a proof field means"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask what a proof field means…"
        />
        <button type="submit" className={styles.button}>Explain this Proof</button>
      </form>
      {status === 'loading' && <p className={styles.state}>Explaining…</p>}
      {status === 'error' && <p className={styles.state} data-testid="explainer-error">Explainer error — try again.</p>}
      {result && status !== 'loading' && (
        <div className={styles.result} data-testid="explainer-result">
          {validityRoute && (
            <p className={styles.route} data-testid="validity-route">
              Deterministic answer — the Verify result + Proof Checks are the source of truth.
            </p>
          )}
          <p className={styles.explanation}>{result.explanation}</p>
          <p className={styles.footer} data-testid="explainer-footer">{result.footer}</p>
        </div>
      )}
    </section>
  );
}
