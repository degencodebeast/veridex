import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { MakerArenaResultResponseWire } from '@/lib/wire';
import { adaptMakerArenaResult } from '@/lib/api';

// The frozen maker envelope lives at the repo root (outside apps/web). Read + parse the REAL sealed
// fixture at test time so the Maker* wire types can never drift from the backend result.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
function load<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIX, name), 'utf8')) as T;
}

// Keys that would betray a directional-CLV / PnL leak into the maker rows (SEC-005). None may appear.
const FORBIDDEN_KEYS = ['avg_clv_bps', 'total_clv_bps', 'sim_pnl', 'pnl'];

describe('maker wire contract binding — maker_arena_result.json parses into Maker* types', () => {
  it('binds the frozen maker envelope (lane / rank axis / direction)', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.schema_version).toBe('maker_arena_result.v1');
    expect(m.lane).toBe('maker');
    expect(m.rank_axis).toBe('avg_toxicity_loss_bps');
    expect(m.rank_axis_direction).toBe('asc');
  });

  it('result carries the sealed rung + small-n universe', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.result.rung).toBe('MM-R1');
    expect(m.result.fixture_universe_n).toBe(18);
    expect(m.result.small_n_flag).toBe(true);
  });

  it('real_executable_edge_bps is ALWAYS null (top-level + every row) — no fill/PnL claim', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.result.real_executable_edge_bps).toBeNull();
    for (const row of m.result.maker_leaderboard) {
      expect(row.real_executable_edge_bps).toBeNull();
    }
  });

  it('maker_leaderboard ranks by avg_toxicity_loss_bps ascending (lower is better)', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    const lb = m.result.maker_leaderboard;
    expect(lb[0].maker_rank).toBe(1);
    expect(lb[0].avg_toxicity_loss_bps).toBeLessThanOrEqual(lb[1].avg_toxicity_loss_bps);
  });

  it('no directional-CLV / PnL key leaks into the maker leaderboard rows (SEC-005)', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    for (const row of m.result.maker_leaderboard) {
      const keys = Object.keys(row);
      for (const forbidden of FORBIDDEN_KEYS) {
        expect(keys).not.toContain(forbidden);
      }
      // positive: the rank axis + maker placement are present, `rank` is NOT.
      expect(keys).toContain('maker_rank');
      expect(keys).toContain('avg_toxicity_loss_bps');
      expect(keys).not.toContain('rank');
    }
  });

  it('diagnostics label markout as diagnostic (NOT the rank axis)', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.diagnostics.avg_markout_bps_label).toBe('diagnostic_not_rank_axis');
  });

  // E5-T2: the surfaced R1.5 trade-aware diagnostic never carries a fill / PnL / edge / CLV
  // key. Null on the sealed MM-R1 seal (never a fabricated value); if ever populated, no
  // forbidden key may appear in its payload.
  it('R1.5 trade-aware diagnostic payload carries no fill/PnL/edge/CLV key', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    const payload = m.result.trade_aware_diagnostic;
    // sealed seal is MM-R1 → null (honest no-data, not a fabricated 0/value)
    expect(payload === null || typeof payload === 'object').toBe(true);
    const banned = [
      'real_executable_edge_bps',
      'executable_edge_bps',
      'fill_rate',
      'fill_price',
      'sim_pnl',
      'pnl',
      'realized_pnl',
      'avg_clv_bps',
      'clv_bps',
    ];
    if (payload !== null) {
      const keys = Object.keys(payload);
      for (const forbidden of banned) {
        expect(keys).not.toContain(forbidden);
      }
    }
  });

  it('proof_card present with rung / n_fixtures / falsification', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.proof_card).toBeDefined();
    expect(m.proof_card.rung).toBe('MM-R1');
    expect(m.proof_card.n_fixtures).toBe(18);
    expect(m.proof_card.falsification.verdict).toBe('SEPARATED');
  });

  // I-R remediation (M3): the sealed configuration identity must survive the adapter — the
  // wire carries result.config_hash and the view-model must preserve it verbatim.
  it('M3: adaptMakerArenaResult preserves the sealed config_hash verbatim', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    expect(m.result.config_hash).toMatch(/^[0-9a-f]{64}$/); // the fixture really carries it
    const view = adaptMakerArenaResult(m);
    expect(view.config_hash).toBe(m.result.config_hash);
  });

  it('adaptMakerArenaResult → Maker view-model (rank/edge/labels preserved, edge stays null)', () => {
    const m = load<MakerArenaResultResponseWire>('maker_arena_result.json');
    const view = adaptMakerArenaResult(m);
    expect(view.lane).toBe('maker');
    expect(view.rank_axis).toBe('avg_toxicity_loss_bps');
    expect(view.source_mode).toBe('replay');
    expect(view.real_executable_edge_bps).toBeNull();
    expect(view.leaderboard[0].maker_rank).toBe(1);
    expect(view.leaderboard[0].real_executable_edge_bps).toBeNull();
    expect(view.leaderboard[0].avg_toxicity_loss_bps).toBeLessThanOrEqual(
      view.leaderboard[1].avg_toxicity_loss_bps,
    );
    expect(view.proof_card.n_fixtures).toBe(18);
    expect(view.diagnostics.avg_markout_bps_label).toBe('diagnostic_not_rank_axis');
  });
});
