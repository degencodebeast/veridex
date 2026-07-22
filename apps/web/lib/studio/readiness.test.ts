import { describe, it, expect } from 'vitest';
import { STRATEGY_TEMPLATES } from '@/lib/studio/templates';
import { templateReadiness } from '@/lib/studio/readiness';

describe('templateReadiness (shared Studio-deploy predicate)', () => {
  const byId = Object.fromEntries(STRATEGY_TEMPLATES.map((t) => [t.id, t]));

  it('pins the readiness of all 8 templates to Studio\'s actual deploy gate', () => {
    expect(templateReadiness(byId.value_vs_venue)).toBe('Locked');     // numeric value_clv, deploy-disabled
    expect(templateReadiness(byId.stale_line)).toBe('Locked');         // numeric stale_line, deploy-disabled
    expect(templateReadiness(byId.momentum)).toBe('Deployable');       // llm
    expect(templateReadiness(byId.contrarian_fade)).toBe('Deployable');// llm
    expect(templateReadiness(byId.arb_spread)).toBe('Locked');         // heavy-extension, !deployable
    expect(templateReadiness(byId.quoteguard_mm)).toBe('Deployable');  // heavy-extension carve-out
    expect(templateReadiness(byId.det_drift)).toBe('Deployable');      // deployable, cumulative-drift
    expect(templateReadiness(byId.llm_drift)).toBe('Arena-only');      // arenaOnly
  });

  it('every template resolves to one of the three honest states', () => {
    for (const t of STRATEGY_TEMPLATES) {
      expect(['Deployable', 'Arena-only', 'Locked']).toContain(templateReadiness(t));
    }
  });
});
