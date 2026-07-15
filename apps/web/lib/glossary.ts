// Central, single-source glossary for the InfoTip primitive. This text IS the doctrine (pinned by
// codex) — screens MUST pull from here, never inline their own microcopy. Do not paraphrase.
export interface GlossaryEntry {
  label: string;
  definition: string;
}

export const GLOSSARY = {
  fair_value: {
    label: 'Fair Value',
    definition: 'TxLINE de-margined, market-implied consensus probability; not guaranteed truth.',
  },
  mispricing_gap: {
    label: 'Mispricing Gap',
    definition: 'TxLINE fair value minus the venue-implied probability; a probability-space dislocation, explanatory only — never an edge, never a score.',
  },
  executable_edge: {
    label: 'Executable Edge',
    definition: 'forward EV at the actual venue price; gates action; never a score.',
  },
  clv: {
    label: 'CLV',
    definition: 'entry vs later closing TxLINE line; primary skill metric; backward-looking.',
  },
  checks_vs_metrics: {
    label: 'Checks vs Metrics',
    definition: 'Checks certify the record; metrics rank performance.',
  },
  proof_mode: {
    label: 'Proof Mode',
    definition: 'reproducible = deterministic strategy rerun regenerates actions/scores; verified = recorded action/evidence recomputed and checked, but LLM/runtime not byte-reproduced; partial = shown for transparency, not fully eligible/complete.',
  },
  source_mode: {
    label: 'Source Mode',
    definition: 'data source — replay or live.',
  },
  execution_mode: {
    label: 'Execution Mode',
    definition: 'order behavior — paper, dry_run, live_guarded.',
  },
  eligibility: {
    label: 'Eligibility',
    definition: 'whether a run/agent can be ranked; not a performance boost.',
  },
  anchor: {
    label: 'Anchor',
    definition: 'Solana commitment to the manifest hash; not a claim that every byte is on-chain.',
  },
  config_pinned: {
    label: 'Config pinned',
    definition: 'this exact config is frozen at create; post-run hashes live on the Proof Card.',
  },
  kelly: {
    label: 'Kelly',
    definition: 'capped policy sizing; never rank/scoring/proof.',
  },
  seq: {
    label: 'Seq',
    definition: 'canonical event order, not wall-clock truth.',
  },
  verifier_version: {
    label: 'Verifier version',
    definition: 'version of the deterministic verifier/law used for recompute.',
  },
  window_clv: {
    label: 'window CLV',
    definition: 'CLV measured against the run window\'s close (fixed_duration/manual_stop), not the true match closing line — never shown as CLV.',
  },
  clv_pending: {
    label: 'pending',
    definition: 'too little runway remains before the window closes to score CLV; excluded from CLV means, never shown as a fabricated number.',
  },
  toxicity_loss: {
    label: 'Toxicity Loss',
    definition: 'mean of per-quote adverse-selection loss; the Maker lane rank axis, lower is better — never CLV.',
  },
  mean_markout_diagnostic: {
    label: 'Mean Markout',
    definition: 'raw two-sided mean ≈ half_spread/ref — geometry, not quality; a diagnostic only, never the rank axis.',
  },
  falsification: {
    label: 'Falsification',
    definition: 'pairwise bootstrap test of the Δ between two agents; SEPARATED = whole 95% CI above zero, INCONCLUSIVE = CI spans zero.',
  },
  maker_small_n: {
    label: 'Maker small-n',
    definition: 'the Maker Arena result is scored on a small fixture universe (n=18); always shown as a caveat, never hidden or rounded away.',
  },
} as const satisfies Record<string, GlossaryEntry>;

export type GlossaryTerm = keyof typeof GLOSSARY;
