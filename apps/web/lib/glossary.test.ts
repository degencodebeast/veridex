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
});
