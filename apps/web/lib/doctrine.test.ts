import { describe, it, expect } from 'vitest';
import { QUANTITIES, STABLE_PRICE_CAPTION } from '@/lib/doctrine';

describe('strategy doctrine (four distinct quantities)', () => {
  it('defines Fair Value, Executable Edge, CLV, and Stake/Kelly as distinct quantities', () => {
    expect(QUANTITIES.map((q) => q.id)).toEqual(['fair_value', 'executable_edge', 'clv', 'stake']);
    expect(QUANTITIES.map((q) => q.label)).toEqual(['Fair Value', 'Executable Edge', 'CLV', 'Stake · Kelly']);
  });

  it('labels Stable Price as market-implied de-margined fair probability, NOT guaranteed truth', () => {
    expect(STABLE_PRICE_CAPTION).toMatch(/de-margined/i);
    expect(STABLE_PRICE_CAPTION).toMatch(/not a guaranteed true probability/i);
  });

  it('keeps CLV as the proven skill metric, separate from executable edge', () => {
    const clv = QUANTITIES.find((q) => q.id === 'clv')!;
    const edge = QUANTITIES.find((q) => q.id === 'executable_edge')!;
    expect(clv.definition).toMatch(/skill/i);
    expect(edge.definition).toMatch(/venue/i);
  });
});
