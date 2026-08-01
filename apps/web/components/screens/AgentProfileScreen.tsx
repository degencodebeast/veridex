'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { ConfBar } from '@/components/ui/ConfBar';
import type { AgentProfileRecord } from '@/lib/catalog';
import styles from './AgentProfileScreen.module.css';

export function AgentProfileScreen({
  profile, onOpenRuntime = () => {},
}: { profile: AgentProfileRecord; onOpenRuntime?: (agentId: string) => void }) {
  return (
    <section className={styles.screen} aria-label={`Agent profile ${profile.agent_name}`}>
      <header className={styles.head}>
        <div>
          <h1 className={styles.title}>{profile.agent_name}</h1>
          <span className={styles.sub}>{profile.archetype} · {profile.mode} · {profile.source}</span>
        </div>
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={() => onOpenRuntime(profile.agent_id)}>⌬ RUNTIME · LOGS →</button>
          <Link href={`/clone?source=${profile.agent_id}`} className={styles.primary}>Clone this agent →</Link>
        </div>
      </header>

      <div className={styles.stats}>
        <div className={styles.stat}><span className={styles.statLabel}>Avg CLV</span><Num value={profile.avg_clv_bps} kind="bps" /></div>
        <div className={styles.stat}><span className={styles.statLabel}>Total CLV</span><Num value={profile.total_clv_bps} kind="bps" /></div>
        <div className={styles.stat}><span className={styles.statLabel}>Runs</span><span className="mono">{profile.runs}</span></div>
        <div className={styles.stat}><span className={styles.statLabel}>Valid %</span><span className="mono">{profile.valid_pct == null ? '—' : `${profile.valid_pct.toFixed(1)}%`}</span></div>
        <div className={styles.stat}><span className={styles.statLabel}>Confidence</span><ConfBar validCount={profile.valid_count} /></div>
        <div className={styles.stat}><span className={styles.statLabel}>Proof / Eligibility</span><span className={styles.badges}><Badge variant={profile.proof_mode} /><Badge variant={profile.eligibility_badge} /></span></div>
      </div>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Strategy</h2>
        <p className={styles.caption} data-testid="strategy-caption">{profile.strategy_caption}</p>
        <p className={styles.pinned}>Pinned to config_hash <span className="mono">{profile.config_hash}</span> · policy_hash <span className="mono">{profile.policy_hash}</span>. Describes configuration; never asserts performance.</p>
      </section>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Completed competitions</h2>
        {profile.completed_competitions.length === 0 ? (
          // Honesty guard: a leaner off-mock profile (breakdown_available === false) has runs>0 but no
          // per-competition breakdown — implying "none yet" would be dishonest, so render an honest
          // "not exposed" note. The mock AGENT_PROFILES fixtures omit the flag ⇒ keep today's copy.
          <p className={styles.empty}>
            {profile.breakdown_available === false
              ? "Per-competition breakdown isn't exposed on the public profile."
              : 'No completed competitions yet.'}
          </p>
        ) : (
          <ul className={styles.list}>
            {profile.completed_competitions.map((c) => (
              <li key={c.run_id} className={styles.row}>
                <Link href={`/proof/${c.run_id}`} className={styles.rowLink}>{c.title} ›</Link>
                <Num value={c.avg_clv_bps} kind="bps" />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.h2}>Anchors &amp; provenance</h2>
        <p className={styles.prov}>{profile.deployment_provenance}</p>
        <ul className={styles.list}>
          {profile.anchors.map((a) => (
            <li key={a.run_id} className={`${styles.row} mono`}>
              <span>{a.run_id}</span><span className={styles.anchorSig}>{a.tx_signature} · slot {a.slot}</span>
            </li>
          ))}
        </ul>
      </section>
    </section>
  );
}
