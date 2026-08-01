import {
  MARKET_FAMILY_KEYS, type MarketFamily, type MarketFamilyKey, type OddsUpdate, type SportNode,
} from '@/lib/catalog';

// CON-040 / AC-010: pre-match snapshot is EMPTY. Always read movement history + the SSE stream.
export function oddsUpdatesPath(fid: number): string {
  return `/odds/updates/${fid}`;
}

export function oddsStreamPath(): string {
  return '/odds/stream';
}

// TxLINE prices are integers, decimal x1000 (1472 => 1.472).
export function decodePrice(raw: number): number {
  return raw / 1000;
}

const FAMILY_LABELS: Record<MarketFamilyKey, string> = {
  '1X2_PARTICIPANT_RESULT': 'Match Result (1X2)',
  'OVERUNDER_PARTICIPANT_GOALS': 'Over / Under Goals',
  'ASIANHANDICAP_PARTICIPANT_GOALS': 'Asian Handicap',
};

// Pack-scoped identity: a replay fixture is (pack_id, fixture_id), NOT fixture_id alone — two packs can
// share an external fixture_id. This composite string keys odds storage / selection / React rows / launch.
export function fixtureKey(packId: string, fixtureId: number): string {
  return `${packId}::${fixtureId}`;
}

// CON-040: closing = decimal of the LAST update before in_running flips. null => pending/—.
// `parameters` scopes the reconstruction to the SAME market line (e.g. line=2.5) so two O/U parameter
// rows sharing an outcome name can never cross-borrow each other's price (defense-in-depth).
export function reconstructClosing(
  updates: OddsUpdate[], outcome: string, family: MarketFamilyKey, parameters?: string | null,
): number | null {
  const preMatch = updates
    .filter((x) => x.market_family === family && (x.market_parameters ?? '') === (parameters ?? '') && !x.in_running)
    .sort((a, b) => a.ts - b.ts);
  for (let i = preMatch.length - 1; i >= 0; i -= 1) {
    const idx = preMatch[i].price_names.indexOf(outcome);
    if (idx >= 0) return decodePrice(preMatch[i].prices[idx]);
  }
  return null;
}

// Group the latest update per family into decimal odds + implied % rows. `reconstructClosing` (default
// true) preserves the LIVE path's reconstructed closing line; set false for the replay projection whose
// source DTO deliberately OMITS closing → every closing renders null (—), never a fabricated number.
export function buildFamilies(
  updates: OddsUpdate[], opts?: { reconstructClosing?: boolean },
): MarketFamily[] {
  const doReconstruct = opts?.reconstructClosing ?? true;
  const families: MarketFamily[] = [];
  for (const key of MARKET_FAMILY_KEYS) {
    const familyUpdates = updates.filter((x) => x.market_family === key);
    if (familyUpdates.length === 0) continue;
    const byParams = new Map<string, OddsUpdate>();
    for (const upd of familyUpdates.sort((a, b) => a.ts - b.ts)) {
      byParams.set(upd.market_parameters ?? '', upd); // keep latest per param row
    }
    families.push({
      key,
      label: FAMILY_LABELS[key],
      rows: [...byParams.values()].map((upd) => ({
        parameters: upd.market_parameters,
        outcomes: upd.price_names.map((name, i) => ({
          name,
          decimal: decodePrice(upd.prices[i]),
          impliedPct: upd.pct[i],
          closing: doReconstruct ? reconstructClosing(familyUpdates, name, key, upd.market_parameters) : null,
        })),
      })),
    });
  }
  return families;
}

// REQ-041: never render markets for an unfeedable sport. Disabled sports are honest, labeled.
export const SPORT_CATALOG: SportNode[] = [
  {
    id: 'soccer', label: '⚽ Soccer', enabled: true,
    competitions: [
      { id: 'world_cup', label: 'World Cup', enabled: true },
      { id: 'intl_friendlies', label: "Int'l Friendlies", enabled: true },
    ],
  },
  {
    id: 'us_cfb', label: '🏈 US College Football', enabled: false,
    disabledReason: 'not in free feed / coming soon', competitions: [],
  },
  {
    id: 'us_cbb', label: '🏀 US College Basketball', enabled: false,
    disabledReason: 'not in free feed / coming soon', competitions: [],
  },
];
