// Single source for the killer-flow deep links (REQ-004 / AC-021).
export function cockpitHref(competitionId: string): string {
  return `/arena/${competitionId}`;
}
export function inspectorHref(runId: string, seq: number | string): string {
  return `/inspector/${runId}/${seq}`;
}
export function proofHref(runId: string): string {
  return `/proof/${runId}`;
}
