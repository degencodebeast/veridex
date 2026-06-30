import { describe, it, expect } from 'vitest';
import { inspectorHref, proofHref, cockpitHref } from '@/lib/deeplinks';

describe('killer-flow deep links (REQ-004 / AC-021)', () => {
  it('builds the AGENT_ACTION -> Inspector link', () => {
    expect(inspectorHref('run_7f3a', 87)).toBe('/inspector/run_7f3a/87');
  });
  it('builds the Inspector -> Proof Card link', () => {
    expect(proofHref('run_7f3a')).toBe('/proof/run_7f3a');
  });
  it('builds the cockpit link', () => {
    expect(cockpitHref('wc-fra-bra')).toBe('/arena/wc-fra-bra');
  });
});
