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

  it('keeps the scored/never-scored boundary explicit: CLV is the scored metric; Executable Edge gates execution but is never scored; Stake/Kelly is never a score', () => {
    const clv = QUANTITIES.find((q) => q.id === 'clv')!;
    const edge = QUANTITIES.find((q) => q.id === 'executable_edge')!;
    const stake = QUANTITIES.find((q) => q.id === 'stake')!;
    expect(clv.definition).toMatch(/scored/i);          // CLV IS scored
    expect(edge.definition).toMatch(/never scored/i);   // edge gates execution, not scored
    expect(stake.definition).toMatch(/never a score/i); // Kelly/Stake never reads as a score
  });
});
