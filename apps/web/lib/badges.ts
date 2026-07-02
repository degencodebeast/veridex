// Status vocabulary + glyphs, per the V4 Status Badges table (PAT-002).
export const BADGE_VARIANTS = [
  'replay', 'live', 'reproducible', 'verified', 'anchored', 'pending',
  'not-anchored', 'valid', 'invalid', 'partial', 'eligible', 'not-eligible', 'llm',
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
};
