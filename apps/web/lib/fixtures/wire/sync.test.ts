import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// DRIFT GUARD. lib/mock.ts imports these wire fixtures at BUILD time, but the app's Docker image
// builds with build.context=./apps/web (compose.coolify.yml) — a self-sufficient context that
// cannot reach repo-root files. So the fixtures are vendored here, next to mock.ts. The canonical
// source of truth remains repo-root contracts/fixtures/*.json (the frozen contract every other
// parse test validates). This test fails the moment a vendored copy drifts from its canonical,
// so the two can never silently diverge. Runs in the full monorepo (has the repo root); it is
// excluded from the Docker image, which only needs the vendored copies.
const CANONICAL = resolve(__dirname, '../../../../../contracts/fixtures');

const VENDORED = [
  'proof_artifact.json',
  'verify_response.json',
  'leaderboard.json',
  'competition_state.json',
  'inspector_record.json',
  'feed_health.json',
  'maker_arena_result.json',
] as const;

describe('wire fixture vendoring — apps/web copies stay byte-identical to the frozen contract', () => {
  for (const name of VENDORED) {
    it(`${name} matches contracts/fixtures/${name}`, () => {
      const vendored = readFileSync(resolve(__dirname, name), 'utf8');
      const canonical = readFileSync(resolve(CANONICAL, name), 'utf8');
      expect(vendored).toBe(canonical);
    });
  }
});
