import { describe, it, expect } from 'vitest';
import { GLOSSARY } from '@/lib/glossary';

describe('glossary (single-source doctrine copy)', () => {
  it('every entry has a non-empty label + definition', () => {
    for (const [term, entry] of Object.entries(GLOSSARY)) {
      expect(entry.label, term).toBeTruthy();
      expect(entry.definition.length, term).toBeGreaterThan(10);
    }
  });

  it('carries the terms the create wizard wires (config_pinned / source_mode / execution_mode / proof_mode)', () => {
    (['config_pinned', 'source_mode', 'execution_mode', 'proof_mode'] as const).forEach((k) =>
      expect(GLOSSARY[k]?.definition).toBeTruthy());
  });

  it('carries mispricing_gap as a DISTINCT probability-space term — explanatory, never labeled "edge" (REQ-2D-501)', () => {
    const gap = GLOSSARY.mispricing_gap;
    expect(gap?.label).toBeTruthy();
    // Probability-space dislocation, explicitly NOT edge (mispricing_gap ≠ executable_edge).
    expect(gap.definition).toMatch(/probability/i);
    expect(gap.definition).toMatch(/never (an )?edge|not.*edge/i);
    // The two terms must read distinctly (prob-space dislocation vs forward EV).
    expect(gap.label).not.toEqual(GLOSSARY.executable_edge.label);
    expect(gap.definition).not.toEqual(GLOSSARY.executable_edge.definition);
  });
});
