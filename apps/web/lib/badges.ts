// Status vocabulary + glyphs, per the V4 Status Badges table (PAT-002).
export const BADGE_VARIANTS = [
  'replay', 'live', 'reproducible', 'verified', 'anchored', 'pending',
  'not-anchored', 'valid', 'invalid', 'partial', 'eligible', 'not-eligible', 'llm', 'final',
  // Roster proof-state (E3) — the honest chips for a public agent's ProofState that are NOT an earned
  // single-mode proof claim: 'unscored' (deployed, not yet scored), 'mixed' (honest cross-run aggregate
  // of different proof modes), 'unknown' (fail-closed fallback). Neutral — NEVER a proof/rank claim.
  'unscored', 'mixed', 'unknown',
  // Maker Arena lane (MM-R1) — falsification verdicts + rung/caveat chips (SEC-005: never
  // reused to imply a directional CLV claim; these back only the maker surfaces).
  'mm-r1', 'separated', 'inconclusive', 'inverted', 'uncalibrated', 'small-n', 'trades-not-fills',
  // QuoteGuard behavior ablation (F-8 · maker_live_ab.v1). These label a BEHAVIOR comparison — they
  // never carry a rank / winner / edge meaning (guard-on/off name the ARM, not a better/worse verdict).
  'behavior-ablation', 'not-a-leaderboard', 'recorded-replay', 'same-strategy-tape',
  'diverges-true', 'diverges-false', 'guard-on', 'guard-off',
  // AC-30/AC-31 — a third-party print / counterfactual capacity ceiling. NEVER a fill/PnL/rank badge.
  'counterfactual',
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
  unscored: { glyph: '○', label: 'Unscored' },
  // Neutral honesty chips — NOT a proof claim. 'mixed' = runs carry different proof modes; 'unknown' =
  // an unrecognized proof_state string (fail-closed, never rendered as an earned proof).
  mixed: { glyph: '◐', label: 'Mixed' },
  unknown: { glyph: '?', label: 'Unknown' },
  eligible: { glyph: '●', label: 'Eligible' },
  'not-eligible': { glyph: '⊘', label: 'Not Eligible' },
  llm: { glyph: '', label: 'LLM' },
  final: { glyph: '■', label: 'Finished' },
  'mm-r1': { glyph: '◆', label: 'MM-R1' },
  separated: { glyph: '✓', label: 'Separated' },
  inconclusive: { glyph: '≈', label: 'Inconclusive' },
  inverted: { glyph: '⇅', label: 'Inverted' },
  uncalibrated: { glyph: '⚠', label: 'Uncalibrated' },
  'small-n': { glyph: '⚠', label: 'Small N' },
  'trades-not-fills': { glyph: '', label: 'Trades ≠ Fills' },
  // Behavior-ablation vocabulary (F-8). The KEYS name the arm/panel; no rank/winner/edge implied.
  'behavior-ablation': { glyph: '', label: 'Behavior Ablation' },
  'not-a-leaderboard': { glyph: '', label: 'Not a Leaderboard' },
  'recorded-replay': { glyph: '⟲', label: 'Recorded TxLINE Replay' },
  'same-strategy-tape': { glyph: '✓', label: 'Same Strategy / Same Tape' },
  'diverges-true': { glyph: '◇', label: 'Diverges: true' },
  'diverges-false': { glyph: '=', label: 'Diverges: false' },
  'guard-on': { glyph: '', label: 'Guard On' },
  'guard-off': { glyph: '', label: 'Guard Off' },
  // Third-party print / counterfactual capacity ceiling — labeled, bounded, never our fill/PnL/rank.
  counterfactual: { glyph: '⋯', label: 'Counterfactual' },
};
