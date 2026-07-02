'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { AGENTS } from '@/lib/fixtures/catalog';
import type { AgentSummary } from '@/lib/catalog';
import styles from './AgentsScreen.module.css';

type Sort = 'clv' | 'runs';

export function AgentsScreen({ agents = AGENTS }: { agents?: AgentSummary[] }) {
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<Sort>('clv');

  const shown = useMemo(() => {
    const filtered = agents.filter((a) => a.agent_name.toLowerCase().includes(q.toLowerCase()));
    return [...filtered].sort((a, b) => (sort === 'clv' ? b.avg_clv_bps - a.avg_clv_bps : b.runs - a.runs));
  }, [agents, q, sort]);

  return (
    <section className={styles.screen} aria-label="Agents Directory">
      <header className={styles.head}>
        <h1 className={styles.title}>Agents</h1>
        <div className={styles.actions}>
          <Link href="/duel" className={styles.secondary}>⚔ Compare Two →</Link>
          <Link href="/studio" className={styles.primary}>+ Create Agent</Link>
        </div>
      </header>

      <div className={styles.controls}>
        <input
          type="search" role="searchbox" className={styles.search} placeholder="Search agents…"
          value={q} onChange={(e) => setQ(e.target.value)}
        />
        <SegmentedControl<Sort>
          ariaLabel="Sort" value={sort} onChange={setSort}
          options={[{ value: 'clv', label: 'Avg CLV' }, { value: 'runs', label: 'Runs' }]}
        />
      </div>

      {shown.length === 0 ? (
        <p className={styles.empty} data-testid="agents-empty">No agents match.</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr><th>AGENT</th><th>ARCHETYPE</th><th>MODE</th><th className={styles.r}>AVG CLV</th><th className={styles.r}>RUNS</th><th>PROOF</th><th>SOURCE</th></tr>
            </thead>
            <tbody>
              {shown.map((a) => (
                <tr key={a.agent_id} className={styles.row}>
                  <td><Link href={`/agents/${a.agent_id}`} className={styles.link}>{a.agent_name} ›</Link></td>
                  <td className="mono">{a.archetype}</td>
                  <td className="mono">{a.mode}</td>
                  <td className={styles.num}><Num value={a.avg_clv_bps} kind="bps" /></td>
                  <td className={styles.num}>{a.runs}</td>
                  <td><Badge variant={a.proof_mode} /></td>
                  <td>
                    {a.source_mode === 'live' ? <Badge variant="live" />
                      : a.source_mode === 'replay' ? <Badge variant="replay" />
                        : <span className={`${styles.mixedSrc} mono`}>mixed</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
