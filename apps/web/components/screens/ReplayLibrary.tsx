import type { ReplayPackView } from '@/lib/api';

// The deployed Replay Library — backed ONLY by GET /replay-packs. Renders the admitted catalog:
// pack_id, captured fixture labels, RAW fixture ids, content_hash, provenance, replay status. It
// invents NO odds, edge, closing values, agent counts, or feed health (spec §5.2). A missing label
// falls back to the raw id + "label unavailable"; an empty catalog is an honest empty state.
export function ReplayLibrary({
  packs,
  onLaunch,
}: {
  packs: ReplayPackView[];
  onLaunch: (packId: string, fixtureId: number) => void;
}) {
  if (packs.length === 0) {
    return (
      <section aria-label="Replay Library">
        <h2>Replay Library</h2>
        <p>No replay packs are admitted on this deployment yet.</p>
      </section>
    );
  }
  return (
    <section aria-label="Replay Library">
      <h2>Replay Library</h2>
      {packs.map((pack) => (
        <article key={pack.packId} data-testid={`replay-pack-${pack.packId}`}>
          <header>
            <h3>{pack.packId}</h3>
            <span className="mono">{pack.provenance}</span>
            <span className="mono">replay {pack.isGenuine ? '· genuine' : ''}</span>
            <span className="mono">hash {pack.contentHash}</span>
          </header>
          <ul>
            {pack.fixtures.map((fixtureId) => {
              const meta = pack.fixtureMetadata.find((m) => m.fixture_id === fixtureId);
              const labelled =
                meta && meta.label_source === 'captured' && meta.home_team && meta.away_team;
              return (
                <li key={fixtureId} data-testid={`replay-fixture-${fixtureId}`}>
                  <span>
                    {labelled ? `${meta!.home_team} v ${meta!.away_team}` : 'label unavailable'}
                  </span>
                  <span className="mono"> · id {fixtureId}</span>
                  <button type="button" onClick={() => onLaunch(pack.packId, fixtureId)}>
                    Launch competition
                  </button>
                </li>
              );
            })}
          </ul>
        </article>
      ))}
    </section>
  );
}
