import { describe, it, expect } from 'vitest';
import { CHECK_IDS, CHECK_LABELS, CHECK_ORDER } from '@/lib/checks';

describe('check taxonomy (SEC-001/002, §4.3)', () => {
  it('freezes exactly the 7 CheckIds (lowercase, matching the frozen contract)', () => {
    expect([...CHECK_IDS].sort()).toEqual(
      ['anchor', 'evidence_integrity', 'llm_boundary', 'manifest_bound',
       'metrics_recomputed', 'policy_obeyed', 'receipt_separation'].sort(),
    );
    expect(CHECK_IDS).toHaveLength(7);
  });

  it('maps metrics_recomputed to the UI label "Score Recomputed"', () => {
    expect(CHECK_LABELS.metrics_recomputed).toBe('Score Recomputed');
  });

  it('contains no metric (CLV) in the check vocabulary', () => {
    const blob = `${CHECK_IDS.join(' ')} ${Object.values(CHECK_LABELS).join(' ')}`;
    expect(blob).not.toMatch(/clv/i);
  });

  it('orders the 7 checks deterministically', () => {
    expect([...CHECK_ORDER].sort()).toEqual([...CHECK_IDS].sort());
  });
});
