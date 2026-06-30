// Frequency-gated motion rules (PAT-003, anti-slop). Encode as enforceable
// constants the screen plans consume — not prose buried in components.
export const PULSE_DURATION_MS = 1400; // live dots: 1.4s pulse, opacity 1 -> .35
export const SCORE_UPDATE_MAX_MS = 150; // score_update transitions cap
export const NO_PER_ROW_ENTRANCE = true; // market-tick/feed rows: never animate entrance
export const MAX_ANIMATED_ELEMENTS_PER_VIEW = 2; // <= 1-2 animated elements per view
export const ANIMATABLE_PROPERTIES = ['transform', 'opacity'] as const;

export const MOTION_RULES: readonly string[] = [
  'Market-tick/feed rows get NO per-row entrance animation.',
  'score_update transitions complete in <= 150ms.',
  'filled / anchor-sealed / competition-finalize are rare and meaningful.',
  'Live dots pulse on a 1.4s cycle, opacity 1 -> .35.',
  'Animate transform/opacity only.',
  'Respect prefers-reduced-motion: disable/reduce all motion.',
  'No decorative animation; <= 1-2 animated elements per view.',
];
