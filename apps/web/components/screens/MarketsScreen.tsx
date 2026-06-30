'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { SPORT_CATALOG, buildFamilies, oddsUpdatesPath } from '@/lib/txline/client';
import { ODDS_UPDATES, FIXTURES } from '@/lib/fixtures/catalog';
import type { FixtureSummary, OddsUpdate, SourceMode } from '@/lib/catalog';
import styles from './MarketsScreen.module.css';

// buildFamilies already DECODES prices to decimal odds and carries a 3dp implied-% string,
// so we format inline: decimal/closing → .toFixed(3), implied % verbatim + '%'. (C1's
// fmtDecimalOdds expects raw milli and fmtPct rounds to 1dp — wrong domain/precision here.)

export function MarketsScreen({
  oddsByFixture = ODDS_UPDATES, fixtures = FIXTURES, sourceMode = 'replay',
}: { oddsByFixture?: Record<number, OddsUpdate[]>; fixtures?: FixtureSummary[]; sourceMode?: SourceMode }) {
  const [sportId, setSportId] = useState('soccer');
  const [fixtureId, setFixtureId] = useState<number | null>(null);

  const updates = fixtureId != null ? oddsByFixture[fixtureId] ?? [] : [];
  const families = useMemo(() => buildFamilies(updates), [updates]);

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
                      <button type="button" data-testid={`fixture-${f.fixture_id}`} className={styles.fixture} onClick={() => setFixtureId(f.fixture_id)}>
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
                <Link href="/competitions/create" className={styles.launch}>Launch a competition from here →</Link>
              </div>
              <div className={styles.families} data-testid="families" data-odds-path={oddsUpdatesPath(fixtureId)}>
                {families.map((fam) => (
                  <div key={fam.key} className={styles.family}>
                    <h3 className={styles.familyTitle}>{fam.label}</h3>
                    {fam.rows.map((row, i) => (
                      <table key={i} className={styles.table}>
                        <thead>
                          <tr>
                            <th>{row.parameters ?? 'OUTCOME'}</th>
                            <th className={styles.r}>ODDS</th>
                            <th className={styles.r}>IMPLIED %</th>
                            <th className={styles.r}>CLOSING</th>
                          </tr>
                        </thead>
                        <tbody>
                          {row.outcomes.map((o) => (
                            <tr key={o.name}>
                              <td>{o.name}</td>
                              <td className={styles.num}>{o.decimal.toFixed(3)}</td>
                              <td className={styles.num}>{o.impliedPct}%</td>
                              <td className={styles.num}>{o.closing == null ? (<span className={styles.pending}>pending / —</span>) : o.closing.toFixed(3)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ))}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
