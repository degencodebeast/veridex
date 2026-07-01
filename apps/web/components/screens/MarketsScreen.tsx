'use client';
import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { SPORT_CATALOG, buildFamilies, oddsUpdatesPath } from '@/lib/txline/client';
import { ODDS_UPDATES, FIXTURES, FEED_HEALTH, LEADERBOARD_ROWS } from '@/lib/fixtures/catalog';
import { isMockEnabled } from '@/lib/mock';
import type {
  FixtureSummary, OddsUpdate, SourceMode, FeedHealthState, LeaderboardRow, MarketFamilyKey,
} from '@/lib/catalog';
import styles from './MarketsScreen.module.css';

// buildFamilies already DECODES prices to decimal odds and carries a 3dp implied-% string,
// so we format inline: decimal/closing → .toFixed(3), implied % verbatim + '%'. (C1's
// fmtDecimalOdds expects raw milli and fmtPct rounds to 1dp — wrong domain/precision here.)

// Market-type tabs (V5). The HT half-time variant is NOT in the free feed → its tab is present
// for layout parity but DISABLED (honest), never a fabricated empty/zero market.
type TabId = 'all' | MarketFamilyKey | 'ht';
const TABS: { id: TabId; label: string; disabled?: boolean; disabledReason?: string; testid: string }[] = [
  { id: 'all', label: 'ALL', testid: 'tab-all' },
  { id: '1X2_PARTICIPANT_RESULT', label: '1X2 FT', testid: 'tab-1x2' },
  { id: 'ht', label: '1X2 HT', disabled: true, disabledReason: 'not in current feed', testid: 'tab-1x2-ht' },
  { id: 'OVERUNDER_PARTICIPANT_GOALS', label: 'O/U', testid: 'tab-ou' },
  { id: 'ASIANHANDICAP_PARTICIPANT_GOALS', label: 'AH', testid: 'tab-ah' },
];

export function MarketsScreen({
  oddsByFixture = ODDS_UPDATES, fixtures = FIXTURES, sourceMode = 'replay',
  feedHealth = FEED_HEALTH, leaderboard = LEADERBOARD_ROWS,
}: {
  oddsByFixture?: Record<number, OddsUpdate[]>; fixtures?: FixtureSummary[]; sourceMode?: SourceMode;
  feedHealth?: FeedHealthState; leaderboard?: LeaderboardRow[];
}) {
  const [sportId, setSportId] = useState('soccer');
  // Default-select the first fixture so the dashboard populates on load (V5) — not an empty prompt.
  const [fixtureId, setFixtureId] = useState<number | null>(fixtures[0]?.fixture_id ?? null);
  const [tab, setTab] = useState<TabId>('all');

  const updates = fixtureId != null ? oddsByFixture[fixtureId] ?? [] : [];
  const allFamilies = useMemo(() => buildFamilies(updates), [updates]);
  const families = tab === 'all' ? allFamilies : allFamilies.filter((f) => f.key === tab);
  const selected = fixtures.find((f) => f.fixture_id === fixtureId) ?? null;
  // ELIGIBLE AGENTS rail = the eligible POOL (badge==='eligible'), NOT scoped to this fixture
  // (no fixture→agent mapping exists). Honest: not-eligible agents are excluded.
  const eligible = useMemo(() => leaderboard.filter((r) => r.eligibility_badge === 'eligible'), [leaderboard]);
  // Mock-gate (hydration-safe). The AGENTS column is a roadmappable demo count shown ONLY under mock
  // (no market_key→agents mapping yet); live stays honest "—". EDGE is NOT gated — it stays "—" even
  // under mock (executable edge is a per-decision Inspector quantity, not a catalog value).
  const [mock, setMock] = useState(false);
  useEffect(() => { setMock(isMockEnabled()); }, []);
  const demoAgents = eligible.length;

  return (
    <section className={styles.screen} aria-label="Markets">
      <h1 className={styles.title}>Markets</h1>
      <div className={styles.layout}>
        <aside className={styles.tree} aria-label="Sport category browser">
          {SPORT_CATALOG.map((s) => (
            <div key={s.id} className={styles.sportBlock}>
              <button
                type="button"
                className={`${styles.sport} ${sportId === s.id ? styles.activeSport : ''}`}
                disabled={!s.enabled}
                onClick={() => s.enabled && setSportId(s.id)}
              >
                {s.label}
              </button>
              {!s.enabled && <span className={styles.disabledReason}>{s.disabledReason}</span>}
              {s.enabled && sportId === s.id && (
                <ul className={styles.comps}>
                  {s.competitions.map((c) => (
                    <li key={c.id} className={styles.comp}>{c.label}</li>
                  ))}
                  {fixtures.map((f) => (
                    <li key={f.fixture_id}>
                      <button
                        type="button"
                        data-testid={`fixture-${f.fixture_id}`}
                        className={`${styles.fixture} ${f.fixture_id === fixtureId ? styles.activeFixture : ''}`}
                        onClick={() => setFixtureId(f.fixture_id)}
                      >
                        {f.participant1} v {f.participant2} {f.in_running ? <Badge variant="live" /> : <Badge variant="pending" />}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </aside>

        <div className={styles.detail}>
          {fixtureId == null ? (
            <p className={styles.empty}>Select a fixture to view consensus odds (decimal Prices + implied %).</p>
          ) : (
            <>
              <div className={styles.feedStrip} data-testid="source-strip">
                <Badge variant={sourceMode === 'live' ? 'live' : 'replay'} />
                <span className={`${styles.feed} mono`}>SOURCE {sourceMode} · TxLINE Stable Price consensus · de-margined</span>
                <Link
                  href={`/competitions/create?fixture=${fixtureId}`}
                  data-testid="launch-competition"
                  className={styles.launch}
                >
                  Launch a competition from here →
                </Link>
              </div>

              <div className={styles.tabs} role="tablist" aria-label="Market type">
                {TABS.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    role="tab"
                    aria-selected={tab === t.id}
                    data-testid={t.testid}
                    disabled={t.disabled}
                    // Disabled tabs state WHY (reuse the disabledReason idiom) — reads "not
                    // available", not "broken". title = hover, aria-label = the accessible name.
                    title={t.disabledReason}
                    aria-label={t.disabledReason ? `${t.label} — ${t.disabledReason}` : undefined}
                    className={`${styles.tab} ${tab === t.id ? styles.activeTab : ''}`}
                    onClick={() => !t.disabled && setTab(t.id)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>

              <div className={styles.families} data-testid="families" data-odds-path={oddsUpdatesPath(fixtureId)}>
                {families.map((fam) => (
                  <div key={fam.key} className={styles.family}>
                    <h3 className={styles.familyTitle}>{fam.label}</h3>
                    {fam.rows.map((row, i) => (
                      <table key={i} className={styles.table}>
                        <thead>
                          <tr>
                            <th scope="col">{row.parameters ? `SELECTION · ${row.parameters}` : 'SELECTION'}</th>
                            <th scope="col" className={styles.r}>CONSENSUS</th>
                            <th scope="col" className={styles.r}>IMPLIED %</th>
                            <th scope="col" className={styles.r}>CLOSING</th>
                            <th scope="col" className={styles.r}>EDGE</th>
                            <th scope="col" className={styles.r}>AGENTS</th>
                          </tr>
                        </thead>
                        <tbody>
                          {row.outcomes.map((o) => (
                            <tr key={o.name}>
                              <td>{o.name}</td>
                              <td className={styles.num}>{o.decimal.toFixed(3)}</td>
                              <td className={styles.num}>{o.impliedPct}%</td>
                              <td className={styles.num}>{o.closing == null ? (<span className={styles.pending}>pending / —</span>) : o.closing.toFixed(3)}</td>
                              {/* EDGE: executable edge needs a venue price (not in this feed) — honest — */}
                              <td className={styles.num} data-testid="edge-cell"><span className={styles.muted}>—</span></td>
                              {/* AGENTS: no per-market agent mapping in the backend — honest — (never a count) */}
                              <td className={styles.num} data-testid="agents-cell">{mock ? demoAgents : <span className={styles.muted}>—</span>}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ))}
                  </div>
                ))}
                <p className={styles.legend}>
                  EDGE is per-decision executable edge (needs a venue price) — see the Decision Inspector.
                  AGENTS per-market counts aren&apos;t tracked yet. Both show — here rather than a fabricated number.
                </p>
              </div>
            </>
          )}
        </div>

        {selected && (
          <aside className={styles.rail} aria-label="Market context">
            <div className={styles.railPanel} data-testid="rail-match-state">
              <h3 className={styles.railTitle}>MATCH STATE</h3>
              <div className={styles.railRow}>{selected.participant1} v {selected.participant2}</div>
              <div className={`${styles.railMeta} mono`}>{selected.competition}</div>
              {/* match-phase (the fixture axis) — NOT a data-source claim (the source lives in the strip/bar) */}
              <div className={`${styles.phase} mono`}>{selected.in_running ? 'IN-PLAY' : 'PRE-MATCH'}</div>
              <div className={`${styles.railMeta} mono`}>kickoff {selected.start_time.slice(0, 10)}</div>
              {/* in-play score/minute are only in the Cockpit WS stream → honest — here */}
              <div className={`${styles.railMeta} mono`}>score — · minute —</div>
            </div>

            <div className={styles.railPanel} data-testid="rail-feed-health">
              <h3 className={styles.railTitle}>FEED HEALTH</h3>
              {/* ws_live drives the label honestly: not a live stream ⇒ OFFLINE, never a fake "live/healthy" */}
              <div className={`${styles.phase} mono`}>{feedHealth.ws_live ? 'LIVE' : 'OFFLINE'}</div>
              <div className={`${styles.railMeta} mono`}>staleness {feedHealth.staleness_s == null ? '—' : `${feedHealth.staleness_s}s`}{feedHealth.stale ? ' · STALE' : ''}</div>
              <div className={`${styles.railMeta} mono`}>ticks {feedHealth.ticks_seen} · events/min {feedHealth.events_per_min ?? '—'}</div>
              <div className={`${styles.railMeta} mono`}>{feedHealth.txline_configured ? 'TxLINE configured' : 'demo feed · TxLINE not configured'}</div>
            </div>

            <div className={styles.railPanel} data-testid="rail-eligible-agents">
              <h3 className={styles.railTitle}>ELIGIBLE AGENTS</h3>
              <div className={`${styles.railMeta} mono`}>eligible pool · fixture-level scoping pending</div>
              <ul className={styles.eligibleList}>
                {eligible.map((a) => (
                  <li key={a.agent_id} className={styles.eligibleRow}>
                    <span>{a.agent_name}</span>
                    <span className={`${styles.num} mono`}>{a.avg_clv_bps >= 0 ? '+' : ''}{a.avg_clv_bps.toFixed(1)} bps</span>
                  </li>
                ))}
              </ul>
            </div>
          </aside>
        )}
      </div>
    </section>
  );
}
