import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { LeaderboardScreen } from '@/components/screens/LeaderboardScreen';
import { adaptLeaderboard } from '@/lib/api';
import type * as W from '@/lib/wire';
import type { LeaderboardRow as CatalogLeaderboardRow } from '@/lib/catalog';

// II-W defect 5 (Major, render path) · The eligibility column must render the BACKEND-authoritative
// `eligibility_badge` (anchor-derived server-side — veridex/leaderboard.py:_eligibility_badge)
// VERBATIM. LeaderboardScreen was RE-DERIVING it from `proof_mode` via isEligible(), reversing the
// adapter fix at render. BACKEND-SHAPED: real wire rows → adaptLeaderboard → rendered table.
function wireRow(over: Partial<W.LeaderboardRow>): W.LeaderboardRow {
  return {
    rank: 1, agent_id: 'agent-x', runs: 3, avg_clv_bps: 10, total_clv_bps: 30, sim_pnl: 30,
    brier: 0.2, max_drawdown: -3, action_count: 6, valid_pct: 100, proof_mode: 'reproducible',
    eligibility_badge: 'unproven', anchor_status: 'none-anchored', source_mode: 'all-replay',
    valid_count: 6, clv_confidence: 'high', low_sample: false, ...over,
  };
}

describe('II-W defect 5 · LeaderboardScreen renders the backend eligibility_badge verbatim, not a proof_mode re-derivation', () => {
  it('a fully-proven+partial-proof_mode row renders ELIGIBLE; an unproven+verified-proof_mode row renders NOT-ELIGIBLE', () => {
    // Two rows where the backend eligibility_badge and proof_mode DISAGREE, so a proof_mode
    // re-derivation (isEligible) would render the OPPOSITE of the backend truth on both.
    const rows = adaptLeaderboard({
      rows: [
        wireRow({ agent_id: 'ALPHA', avg_clv_bps: 25, eligibility_badge: 'fully-proven', proof_mode: 'partial' }),
        wireRow({ agent_id: 'BETA', avg_clv_bps: 5, eligibility_badge: 'unproven', proof_mode: 'verified' }),
      ],
    }) as unknown as CatalogLeaderboardRow[];
    render(<LeaderboardScreen rows={rows} />);
    const tableRows = screen.getAllByTestId('lb-row');
    const provenRow = tableRows.find((r) => within(r).queryByText(/ALPHA/)) as HTMLElement;
    const unprovenRow = tableRows.find((r) => within(r).queryByText(/BETA/)) as HTMLElement;
    expect(provenRow).toBeTruthy();
    expect(unprovenRow).toBeTruthy();
    // backend fully-proven ⇒ eligible (NOT the proof_mode=partial re-derivation of not-eligible)
    expect(provenRow.querySelector('[data-variant="eligible"]')).not.toBeNull();
    expect(provenRow.querySelector('[data-variant="not-eligible"]')).toBeNull();
    // backend unproven ⇒ not-eligible (NOT the proof_mode=verified re-derivation of eligible)
    expect(unprovenRow.querySelector('[data-variant="not-eligible"]')).not.toBeNull();
    expect(unprovenRow.querySelector('[data-variant="eligible"]')).toBeNull();
  });
});
