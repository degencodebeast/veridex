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
} as const satisfies Record<string, GlossaryEntry>;

export type GlossaryTerm = keyof typeof GLOSSARY;
