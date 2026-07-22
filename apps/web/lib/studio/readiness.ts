import { resolveMode, resolveStrategy } from '@/lib/studio/coupling';
import type { StrategyTemplate } from '@/lib/studio/templates';

export type Readiness = 'Deployable' | 'Arena-only' | 'Locked';

// The ONE shared readiness predicate (spec §5.3 / §14 note 2). Agents AND Studio call this — it is
// derived from Studio's ACTUAL deploy gate (resolveMode → resolveStrategy), never a simplified
// re-derivation. Precedence:
//   1. arenaOnly === true                              → Arena-only
//   2. deploy-disabled (heavy-extension && !deployable, OR resolveStrategy yields no backend strategy)
//                                                       → Locked
//   3. else                                            → Deployable
export function templateReadiness(t: StrategyTemplate): Readiness {
  if (t.arenaOnly === true) return 'Arena-only';
  if (t.complexity === 'heavy-extension' && !t.deployable) return 'Locked';
  const mode = resolveMode(t.archetype, t.defaultMode);
  const resolution = resolveStrategy(t.id, t.archetype, mode);
  if (!resolution.supported) return 'Locked';
  return 'Deployable';
}
