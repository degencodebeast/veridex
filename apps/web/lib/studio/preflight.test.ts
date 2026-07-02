import { describe, it, expect } from 'vitest';
import { buildPreflightPreview, PREFLIGHT_DISCLAIMER } from '@/lib/studio/preflight';
import { DEFAULT_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';

// Studio preflight is FULLY (A) real config (codex option 3): it shows the real min_edge THRESHOLD
// from the policy envelope + the rule-config table + a plain disclaimer. It NEVER shows a computed/
// estimated pre-run edge value (that's a per-run sealed proof quantity — same risk class as Markets
// EDGE). `preview_edge_bps` is dropped entirely; no mock-gate.
describe('studio preflight preview (fully-A config: real min-edge threshold + disclaimer, no computed edge)', () => {
  it('exposes the REAL min-edge threshold + rule-config; NO computed/estimated edge value', () => {
    const p = buildPreflightPreview(DEFAULT_POLICY_ENVELOPE);
    // the threshold is the real policy config value, shown as "Minimum executable edge ≥ N bps"
    expect(p.min_edge_threshold_bps).toBe(DEFAULT_POLICY_ENVELOPE.min_edge_bps);
    expect(p.policy_envelope.min_edge_bps).toBe(DEFAULT_POLICY_ENVELOPE.min_edge_bps);
    expect(p.rule_config.length).toBeGreaterThan(0);
    expect(p.rule_config.some((r) => /min_edge/i.test(r.field))).toBe(true);
    // NO computed edge value on the preview — the edge-estimate field is gone (option 3).
    expect('preview_edge_bps' in p).toBe(false);
  });

  it('carries the plain threshold disclaimer with NO proof/verification vocabulary attached to it', () => {
    expect(PREFLIGHT_DISCLAIMER).toMatch(/threshold config only/i);
    expect(PREFLIGHT_DISCLAIMER).toMatch(/recomputed during the run/i); // the edge is a per-RUN quantity
    // the disclaimer must not CLAIM the preview itself is verified/proven/eligible/a score/CLV.
    expect(PREFLIGHT_DISCLAIMER).not.toMatch(/\b(verified|proven|eligible|policy-approved|law result)\b/i);
    expect(PREFLIGHT_DISCLAIMER).not.toMatch(/\b(CLV|score)\b/i);
  });
});
