import type { CompetitionConfigPayload } from '@/lib/api';
import type { ExecutionMode } from '@/lib/catalog';

// Build the exact CompetitionConfig POST /competitions freezes, carrying the authoritative catalog
// identity (pack_id + fixture_id) through — NOT just free-form market_scope text. Absent identity is
// OMITTED (undefined), never fabricated as 0/"".
export function buildCompetitionConfig(input: {
  competitionType: string;
  sourceMode: 'replay' | 'live';
  executionMode: ExecutionMode;
  marketScope: string;
  scoringWindow: string | null;
  rosterSize: number;
  packId: string | null;
  fixtureId: number | null;
}): CompetitionConfigPayload {
  const config: CompetitionConfigPayload = {
    competition_type: input.competitionType,
    source_mode: input.sourceMode,
    execution_mode: input.executionMode,
    market_scope: input.marketScope,
    scoring_window: input.scoringWindow,
    roster_size: Math.max(2, input.rosterSize),
  };
  if (input.packId) config.pack_id = input.packId;
  if (input.fixtureId != null) config.fixture_id = input.fixtureId;
  return config;
}
