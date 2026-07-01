'use client';
import { useState } from 'react';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { Badge } from '@/components/ui/Badge';
import { DEFAULT_POLICY_ENVELOPE, FIXTURES } from '@/lib/fixtures/catalog';
import { MARKET_FAMILY_KEYS } from '@/lib/catalog';
import type { CompetitionType, ExecutionMode, ProofMode, MarketFamilyKey } from '@/lib/catalog';
import styles from './CreateCompetitionScreen.module.css';

type SourceMode = 'replay' | 'live';

function proofFor(type: CompetitionType, source: SourceMode): ProofMode {
  if (source === 'replay' || type === 'replay_arena') return 'reproducible';
  return 'verified';
}

// The 4 real competition_type enum values (veridex CompetitionConfig) as rich cards.
const TYPE_CARDS: { type: CompetitionType; label: string; blurb: string }[] = [
  { type: 'live_arena', label: 'Live Arena', blurb: 'Agents trade a live TxLINE fixture in real time.' },
  { type: 'replay_arena', label: 'Replay Arena', blurb: 'Deterministic replay of a recorded fixture window.' },
  { type: 'head_to_head', label: 'Head-to-Head', blurb: 'Two agents, identical evidence — CLV gap, no winner badge.' },
  { type: 'prize_vault_challenge', label: 'Prize-Vault Challenge', blurb: 'Designed prize target (Phase 2D · no funds move).' },
];

// Short labels for the REAL market families (never invented markets) → composed into market_scope.
const MARKET_LABEL: Record<MarketFamilyKey, string> = {
  '1X2_PARTICIPANT_RESULT': '1X2',
  'OVERUNDER_PARTICIPANT_GOALS': 'O/U',
  'ASIANHANDICAP_PARTICIPANT_GOALS': 'AH',
};

export interface CreateCompetitionCommit {
  competition_type: CompetitionType;
  source_mode: SourceMode;
  execution_mode: ExecutionMode;
  // The wizard commits EXACTLY the fields POST /competitions freezes into CompetitionConfig
  // (SEC-009 "frozen at entry"): type/source/exec + market_scope + scoring_window. proof_mode is
  // deterministic from type+source and travels in the pinned block.
  market_scope: string;
  scoring_window: string | null;
  proof_mode: ProofMode;
}

export function CreateCompetitionScreen({
  onCommit = () => {}, initialFixtureId,
}: { onCommit?: (cfg: CreateCompetitionCommit) => void; initialFixtureId?: number }) {
  const [type, setType] = useState<CompetitionType>('live_arena');
  const [source, setSource] = useState<SourceMode>('live');
  const [exec, setExec] = useState<ExecutionMode>('paper');
  const [fixtureId, setFixtureId] = useState<number>(
    FIXTURES.some((f) => f.fixture_id === initialFixtureId) ? (initialFixtureId as number) : (FIXTURES[0]?.fixture_id ?? 0),
  );
  const [markets, setMarkets] = useState<Set<MarketFamilyKey>>(new Set(MARKET_FAMILY_KEYS));
  const [scoringWindow, setScoringWindow] = useState('');

  const proof = proofFor(type, source);
  const selectedFixture = FIXTURES.find((f) => f.fixture_id === fixtureId) ?? FIXTURES[0];
  const marketKeys = MARKET_FAMILY_KEYS.filter((k) => markets.has(k));
  const fixtureScope = selectedFixture ? `${selectedFixture.participant1} v ${selectedFixture.participant2}` : '';
  // market_scope is the single free-form selector POST accepts (e.g. "FRA v BRA · 1X2 / O/U / AH").
  const market_scope = [fixtureScope, marketKeys.map((k) => MARKET_LABEL[k]).join(' / ')].filter(Boolean).join(' · ');
  const scoring_window = scoringWindow.trim() || null;

  const toggleMarket = (k: MarketFamilyKey) =>
    setMarkets((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });

  const commit = () => onCommit({
    competition_type: type, source_mode: source, execution_mode: exec,
    market_scope, scoring_window, proof_mode: proof,
  });

  return (
    <section className={styles.screen} aria-label="Create Competition">
      <h1 className={styles.title}>Create Competition</h1>
      <div className={styles.layout}>
        <div className={styles.main}>
          {/* 01 — TYPE (rich cards) + source/exec axes */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>01</span> Type &amp; mode</h2>
            <div className={styles.cards} data-testid="type-cards">
              {TYPE_CARDS.map((c) => (
                <button
                  key={c.type}
                  type="button"
                  data-testid={`type-${c.type}`}
                  aria-pressed={type === c.type}
                  className={`${styles.card} ${type === c.type ? styles.cardActive : ''}`}
                  onClick={() => setType(c.type)}
                >
                  <span className={styles.cardLabel}>{c.label}</span>
                  <span className={styles.cardBlurb}>{c.blurb}</span>
                </button>
              ))}
            </div>
            <div className={styles.controls}>
              <label className={styles.field}>
                <span className={styles.label}>Source</span>
                <SegmentedControl<SourceMode>
                  ariaLabel="Source mode" value={source} onChange={setSource}
                  options={[{ value: 'live', label: 'Live' }, { value: 'replay', label: 'Replay' }]}
                />
              </label>
              <label className={styles.field}>
                <span className={styles.label}>Execution</span>
                <SegmentedControl<ExecutionMode>
                  ariaLabel="Execution mode" value={exec} onChange={setExec}
                  options={[{ value: 'paper', label: 'Paper' }, { value: 'dry_run', label: 'Dry Run' }, { value: 'live_guarded', label: 'Live Guarded' }]}
                />
              </label>
            </div>
          </section>

          {/* 02 — FIXTURE & SCORING WINDOW */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>02</span> Fixture &amp; scoring window</h2>
            <div className={styles.controls}>
              <label className={styles.field}>
                <span className={styles.label}>Fixture</span>
                <select
                  className={styles.select} aria-label="Fixture" data-testid="fixture-select"
                  value={fixtureId} onChange={(e) => setFixtureId(Number(e.target.value))}
                >
                  {FIXTURES.map((f) => (
                    <option key={f.fixture_id} value={f.fixture_id}>{f.participant1} v {f.participant2} · {f.competition}</option>
                  ))}
                </select>
              </label>
              <label className={styles.field}>
                <span className={styles.label}>Scoring window (optional)</span>
                <input
                  className={styles.input} type="text" data-testid="scoring-window"
                  placeholder="ISO-8601 duration, e.g. PT90M — blank = full match"
                  value={scoringWindow} onChange={(e) => setScoringWindow(e.target.value)}
                />
              </label>
            </div>
          </section>

          {/* 03 — MARKET SCOPE (real families only) */}
          <section className={styles.section}>
            <h2 className={styles.h2}><span className={styles.n}>03</span> Market scope</h2>
            <div className={styles.checks} data-testid="market-scope">
              {MARKET_FAMILY_KEYS.map((k) => (
                <label key={k} className={styles.check} data-testid={`market-${k}`}>
                  <input type="checkbox" checked={markets.has(k)} onChange={() => toggleMarket(k)} aria-label={MARKET_LABEL[k]} />
                  <span>{MARKET_LABEL[k]}</span>
                </label>
              ))}
            </div>
          </section>
        </div>

        {/* SUMMARY sidebar — the real CompetitionConfig POST /competitions freezes (SEC-009). */}
        <aside className={styles.pinned} data-testid="pinned-config" aria-label="Pinned configuration">
          <h2 className={styles.h2}>Pinned before entry</h2>
          <dl className={styles.summary}>
            <div className={styles.sumRow}><dt>TYPE</dt><dd data-testid="summary-type">{TYPE_CARDS.find((c) => c.type === type)?.label}</dd></div>
            <div className={styles.sumRow}><dt>SOURCE</dt><dd data-testid="summary-source"><Badge variant={source === 'live' ? 'live' : 'replay'} /></dd></div>
            <div className={styles.sumRow}><dt>EXEC</dt><dd data-testid="summary-exec" className="mono">{exec}</dd></div>
            <div className={styles.sumRow}><dt>MARKET SCOPE</dt><dd data-testid="summary-market-scope" className="mono">{market_scope || '—'}</dd></div>
            <div className={styles.sumRow}><dt>SCORING WINDOW</dt><dd data-testid="summary-scoring-window" className="mono">{scoring_window ?? 'full match'}</dd></div>
            <div className={styles.sumRow}><dt>PROOF</dt><dd><Badge variant={proof} /></dd></div>
            <div className={styles.sumRow}><dt>POLICY</dt><dd className="mono">min_edge {DEFAULT_POLICY_ENVELOPE.min_edge_bps} bps · kill {String(DEFAULT_POLICY_ENVELOPE.kill_switch)}</dd></div>
            {/* law_hash row intentionally OMITTED: POST /competitions surfaces no hash at create
                (config_hash is per-agent at registration; policy_hash at run-start). Drop the honest
                treatment in here once decided — no placeholder/fake hash. */}
          </dl>
          <div className={styles.pins}>
            <span className={styles.pin}>LAW deterministic recompute</span>
          </div>
          <p className={styles.note}>These are frozen at entry. Changing config after a run starts creates a new version (SEC-009).</p>
          <button type="button" className={styles.commit} onClick={commit}>Commit &amp; Enter →</button>
        </aside>
      </div>
    </section>
  );
}
