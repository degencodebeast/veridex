import { describe, it, expect } from 'vitest';
import {
  PULSE_DURATION_MS,
  SCORE_UPDATE_MAX_MS,
  NO_PER_ROW_ENTRANCE,
  MAX_ANIMATED_ELEMENTS_PER_VIEW,
  ANIMATABLE_PROPERTIES,
  MOTION_RULES,
} from '@/lib/motion';

describe('motion frequency-gating rules (PAT-003)', () => {
  it('pins the gating constants', () => {
    expect(PULSE_DURATION_MS).toBe(1400);
    expect(SCORE_UPDATE_MAX_MS).toBe(150);
    expect(NO_PER_ROW_ENTRANCE).toBe(true);
    expect(MAX_ANIMATED_ELEMENTS_PER_VIEW).toBe(2);
  });

  it('only allows transform/opacity to be animated', () => {
    expect([...ANIMATABLE_PROPERTIES].sort()).toEqual(['opacity', 'transform']);
  });

  it('documents the anti-slop rules', () => {
    expect(MOTION_RULES.join(' ')).toMatch(/no per-row entrance/i);
    expect(MOTION_RULES.join(' ')).toMatch(/prefers-reduced-motion/i);
  });
});
