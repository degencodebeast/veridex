import { describe, it, expect } from 'vitest';
import { signClass, fmtBps, fmtDecimalOdds, fmtPct, shortHash } from '@/lib/format';

describe('format helpers (GUD-001 numeric direction; REQ-042 decimal odds)', () => {
  it('classifies numeric direction by sign (color = sign of value)', () => {
    expect(signClass(14)).toBe('pos');
    expect(signClass(-3.2)).toBe('neg');
    expect(signClass(0)).toBe('zero');
  });

  it('formats bps with explicit sign', () => {
    expect(fmtBps(18)).toBe('+18.0 bps');
    expect(fmtBps(-4.5)).toBe('-4.5 bps');
    expect(fmtBps(0)).toBe('0.0 bps');
  });

  it('formats TxLINE integer prices (decimal x1000) as decimal odds', () => {
    expect(fmtDecimalOdds(1472)).toBe('1.472');
    expect(fmtDecimalOdds(3121)).toBe('3.121');
  });

  it('formats implied pct strings to one decimal', () => {
    expect(fmtPct('67.935')).toBe('67.9%');
  });

  it('shortens a hash for dense display', () => {
    expect(shortHash('0xabcdef1234567890')).toBe('0xabcd…7890');
  });
});
