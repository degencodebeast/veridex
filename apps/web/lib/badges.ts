// Status vocabulary + glyphs, per the V4 Status Badges table (PAT-002).
export const BADGE_VARIANTS = [
  'replay', 'live', 'reproducible', 'verified', 'anchored', 'pending',
  'not-anchored', 'valid', 'invalid', 'partial', 'eligible', 'not-eligible', 'llm',
  // Maker Arena lane (MM-R1) — falsification verdicts + rung/caveat chips (SEC-005: never
  // reused to imply a directional CLV claim; these back only the maker surfaces).
  'mm-r1', 'separated', 'inconclusive', 'inverted', 'uncalibrated', 'small-n', 'trades-not-fills',
] as const;

export type BadgeVariant = (typeof BADGE_VARIANTS)[number];

export const BADGE_META: Record<BadgeVariant, { glyph: string; label: string }> = {
  replay: { glyph: '⟲', label: 'Replay' },
  live: { glyph: '', label: 'Live' }, // dot is rendered by <LiveDot>
  reproducible: { glyph: '', label: 'Reproducible' },
  verified: { glyph: '', label: 'Verified' },
  anchored: { glyph: '◆', label: 'Anchored' },
  pending: { glyph: '◇', label: 'Pending' },
  'not-anchored': { glyph: '○', label: 'Not Anchored' },
  valid: { glyph: '', label: 'Valid' },
  invalid: { glyph: '', label: 'Invalid' },
  partial: { glyph: '', label: 'Partial' },
  eligible: { glyph: '●', label: 'Eligible' },
  'not-eligible': { glyph: '⊘', label: 'Not Eligible' },
  llm: { glyph: '', label: 'LLM' },
  'mm-r1': { glyph: '◆', label: 'MM-R1' },
  separated: { glyph: '✓', label: 'Separated' },
  inconclusive: { glyph: '≈', label: 'Inconclusive' },
  inverted: { glyph: '⇅', label: 'Inverted' },
  uncalibrated: { glyph: '⚠', label: 'Uncalibrated' },
  'small-n': { glyph: '⚠', label: 'Small N' },
  'trades-not-fills': { glyph: '', label: 'Trades ≠ Fills' },
};
