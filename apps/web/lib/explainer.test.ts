import { describe, it, expect } from 'vitest';
import { isValidityQuestion, validityTemplate } from '@/lib/explainer';

// The client-side validity detector is the Layer-1 belt: a validity/pass/certify-INTENT
// question must short-circuit to the fixed template and NEVER reach the LLM. These teeth
// widen the intent coverage while KEEPING educational questions flowing to the explainer.
describe('isValidityQuestion — validity-intent short-circuit (Layer-1)', () => {
  it('catches the core validity phrasings', () => {
    for (const q of [
      'is this run valid?',
      'was this verified?',
      'did it pass?',
      'is this legit?',
      'can you certify this run?',
      'is this proof genuine?',
    ]) {
      expect(isValidityQuestion(q)).toBe(true);
    }
  });

  it('catches the WIDENED validity-intent phrasings (succeed/reliable/accurate/hold up/real/fake/cheat)', () => {
    for (const q of [
      'did this run succeed?',
      'is this strategy reliable?',
      'is this CLV accurate?',
      'does this proof hold up?',
      'is this real?',
      'is this fake?',
      'did the agent cheat?',
    ]) {
      expect(isValidityQuestion(q)).toBe(true);
    }
  });

  it('does NOT over-catch an educational "correct reading" question (bare "correct" scoped)', () => {
    // This is an educational question about interpreting a field — it must reach the LLM,
    // NOT short-circuit to the validity template.
    expect(isValidityQuestion('what is the correct reading of CLV?')).toBe(false);
    // But a direct validity phrasing with "correct" still short-circuits.
    expect(isValidityQuestion('is this correct?')).toBe(true);
    expect(isValidityQuestion('is it correct?')).toBe(true);
  });

  it('does NOT catch plain educational field questions', () => {
    for (const q of [
      'what does executable edge mean?',
      'explain the anchor field',
      'how is CLV computed?',
    ]) {
      expect(isValidityQuestion(q)).toBe(false);
    }
  });
});

describe('validityTemplate', () => {
  it('cites the deterministic Verify state and never certifies itself', () => {
    expect(validityTemplate(true)).toMatch(/deterministic verify result says: verified/i);
    expect(validityTemplate(false)).toMatch(/NOT verified/);
    expect(validityTemplate(null)).toMatch(/not yet run/i);
    expect(validityTemplate(true)).toMatch(/cannot verify or certify runs/i);
  });
});
