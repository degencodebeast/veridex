// Strategy doctrine — four DISTINCT quantities, the single source of this UI copy.
// Stable Price is the market-implied de-margined FAIR probability, NOT a guaranteed
// "true probability". CLV is the proven skill metric; Executable Edge is EV at the
// actual venue price; Stake/Kelly is Policy sizing.
export type QuantityId = 'fair_value' | 'executable_edge' | 'clv' | 'stake';

export interface QuantityDef {
  id: QuantityId;
  label: string;
  definition: string;
}

export const QUANTITIES: readonly QuantityDef[] = [
  { id: 'fair_value', label: 'Fair Value', definition: 'TxLINE de-margined consensus probability — the market-implied fair probability, not guaranteed truth.' },
  { id: 'executable_edge', label: 'Executable Edge', definition: 'Forward EV at the actual venue decimal price — gates execution, never scored.' },
  { id: 'clv', label: 'CLV', definition: 'Closing-Line Value — the backward, at-close skill metric the law recomputes from sealed evidence. The ONLY scored quantity.' },
  { id: 'stake', label: 'Stake · Kelly', definition: 'Policy sizing (Kelly fraction under the PolicyEnvelope) — never a score or skill metric.' },
] as const;

export const STABLE_PRICE_CAPTION =
  'Stable Price is TxLINE’s market-implied de-margined fair probability — not a guaranteed true probability.';
