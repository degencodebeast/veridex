import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { GuardAblationScreen } from '@/components/screens/proof/GuardAblationScreen';
import GuardAblationPage from '@/app/(app)/proof/maker-ablation/[instanceId]/page';
import type { DeployedInstance } from '@/lib/api';
import type { GuardAblationView } from '@/lib/contracts';

// A minimal DeployedInstance carrying the CURATED label fields the panel best-effort surfaces.
function labeledInstance(over: Partial<DeployedInstance> = {}): DeployedInstance {
  return {
    instance_id: 'mm-inst-0f74a4', template_id: 'quoteguard-mm', agent_id: 'studio-mm',
    run_id: 'run_mm_01', status: 'sealed', source_mode: 'replay', execution_mode: 'paper',
    config_hash: 'c'.repeat(64), policy_hash: 'p'.repeat(64), operator_id: 'did:privy:owner-1',
    runtime_handle: null, last_failure_reason: null,
    market_allowlist: ['pmxt:18209181:home_win'], venue_allowlist: ['polymarket'],
    created_at: '2026-07-17T00:00:00Z',
    fixture_id: 18209181, fixture_label: 'France v Morocco', market_label: 'Home win',
    replay_pack_content_hash: 'f16c3853pack', replay_pack_id: 'pmxt-txline-mm-18209181-v1',
    maker_tape_ref: 'pmxt-txline-mm-18209181-v1', maker_tape_content_hash: '19b314abtape',
    ...over,
  };
}

function divergentView(over: Partial<GuardAblationView> = {}): GuardAblationView {
  return {
    schema_version: 'maker_live_ab.v1', lane: 'maker', panel: 'guard_on_off_ablation', is_ablation: true,
    instance_id: 'mm-inst-0f74a4', mode: 'replay',
    guard_off: {
      guard_enabled: false, terminal_reason: 'tape_exhausted', observations_consumed: 1024,
      decisions: [
        { index: 211, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [{ kind: 'BID', role: 'maker', price: 2.34, post_only: true }] },
        { index: 212, kind: 'QUOTE', reason_codes: ['wide_ok'], legs: [{ kind: 'BID', role: 'maker', price: 2.34, post_only: true }, { kind: 'ASK', role: 'maker', price: 2.51, post_only: true }] },
      ],
    },
    guard_on: {
      guard_enabled: true, terminal_reason: 'guard_halt', observations_consumed: 1024,
      decisions: [
        { index: 211, kind: 'QUOTE', reason_codes: ['spread_ok'], legs: [{ kind: 'BID', role: 'maker', price: 2.34, post_only: true }] },
        { index: 212, kind: 'SUPPRESS', reason_codes: ['guard_toxicity_block'], legs: [] },
      ],
    },
    divergent_frame_indices: [212], diverges: true, labels: {},
    ...over,
  };
}

function convergedView(): GuardAblationView {
  const v = divergentView();
  return {
    ...v, diverges: false, divergent_frame_indices: [],
    guard_on: { ...v.guard_off, guard_enabled: true, terminal_reason: 'tape_exhausted' },
  };
}

const PROHIBITED = /\b(CLV|PnL|ROI|edge|toxicity|winner|rank|placement|profit)\b/i;

describe('GuardAblationScreen (F-8 · maker_live_ab.v1)', () => {
  it('loading → skeletons only, no fabricated values', () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={() => new Promise(() => {})} />);
    expect(screen.getByTestId('ablation-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('ablation-timeline')).toBeNull();
  });

  it('populated + divergent → arms, diverges chip, synchronized timeline with the divergent frame flagged', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(divergentView())} />);
    expect(await screen.findByTestId('ablation-timeline')).toBeInTheDocument();
    expect(screen.getByTestId('ablation-diverges')).toHaveTextContent(/DIVERGES: true/i);
    expect(screen.getByTestId('ablation-diverges')).toHaveTextContent(/\[212\]/);
    // both arms honestly labeled by guard flag
    expect(within(screen.getByTestId('ablation-arm-off')).getByText(/guard_enabled: false/)).toBeInTheDocument();
    expect(within(screen.getByTestId('ablation-arm-on')).getByText(/guard_enabled: true/)).toBeInTheDocument();
    // the divergent frame row is marked divergent
    expect(screen.getByTestId('ablation-frame-212')).toHaveAttribute('data-divergent', 'true');
    expect(screen.getByTestId('ablation-frame-211')).not.toHaveAttribute('data-divergent');
  });

  it('divergent frame expands to per-arm leg detail; a suppressed arm honestly shows "no legs"', async () => {
    const user = userEvent.setup();
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(divergentView())} />);
    await screen.findByTestId('ablation-timeline');
    // frame 212 is open by default (first divergent) → ON arm suppressed = no legs
    const legs = screen.getByTestId('ablation-legs-212');
    expect(within(legs).getByText(/OFF · legs \(2\)/)).toBeInTheDocument();
    expect(within(legs).getByText(/ON · legs \(0\)/)).toBeInTheDocument();
    expect(within(legs).getByText(/quote suppressed/i)).toBeInTheDocument();
    // toggling a non-divergent frame opens its detail
    await user.click(screen.getByTestId('ablation-frame-211'));
    expect(screen.getByTestId('ablation-legs-211')).toBeInTheDocument();
  });

  it('populated + diverges=false → "No behavioral divergence on this replay", no timeline', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(convergedView())} />);
    expect(await screen.findByTestId('ablation-converged')).toHaveTextContent(/No behavioral divergence on this replay/i);
    expect(screen.getByTestId('ablation-converged')).toHaveTextContent(/DIVERGES: false/i);
    expect(screen.queryByTestId('ablation-timeline')).toBeNull();
  });

  it('unavailable (backend 404 → null) → honest "No recorded ablation is available", no values', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(null)} />);
    expect(await screen.findByTestId('ablation-unavailable')).toHaveTextContent(/No recorded ablation is available for this instance/i);
    expect(screen.getByTestId('ablation-unavailable')).toHaveTextContent(/mm-inst-0f74a4/);
    expect(screen.queryByTestId('ablation-arm-off')).toBeNull();
  });

  it('error → RETRY with no values; retry re-invokes the loader', async () => {
    const user = userEvent.setup();
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce(divergentView());
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={loader} />);
    expect(await screen.findByTestId('ablation-error')).toBeInTheDocument();
    expect(screen.queryByTestId('ablation-timeline')).toBeNull();
    await user.click(screen.getByTestId('ablation-retry'));
    expect(await screen.findByTestId('ablation-timeline')).toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('renders the honest not_anchored anchor status — an absent anchor is never implied present', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(divergentView())} />);
    expect(await screen.findByTestId('ablation-anchor')).toHaveTextContent('not_anchored');
  });

  it('always shows the interpretation note; NEVER shows rank / CLV / PnL / edge / toxicity / winner', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(divergentView())} />);
    const note = await screen.findByTestId('ablation-note');
    expect(note).toHaveTextContent(/does not establish profitability, edge, or rank/i);
    // The rendered DATA cells carry none of the prohibited scoring vocabulary as a value or column
    // (glossary disclaimer tooltips legitimately say "not an edge/rank" — those are not data values).
    const table = screen.getByRole('table');
    expect(table.textContent ?? '').not.toMatch(PROHIBITED);
    // arm summary VALUES (the <dd> cells) are guard flag / terminal_reason / counts — no metrics
    screen.getAllByRole('definition').forEach((dd) => expect(dd.textContent ?? '').not.toMatch(PROHIBITED));
  });

  it('labels the recorded replay honestly and never LIVE', async () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4" loadAblation={vi.fn().mockResolvedValue(divergentView())} />);
    const identity = await screen.findByTestId('ablation-identity');
    expect(identity).toHaveTextContent(/RECORDED TxLINE REPLAY/i);
    expect(identity.textContent ?? '').not.toMatch(/\bLIVE\b/);
  });

  it('back-link returns to the deployed-instance page (instance domain) and NEVER the public maker card', () => {
    render(<GuardAblationScreen instanceId="mm-inst-0f74a4" backHref="/instances/mm-inst-0f74a4" loadAblation={() => new Promise(() => {})} />);
    const back = screen.getByRole('link', { name: /deployed instance/i });
    expect(back).toHaveAttribute('href', '/instances/mm-inst-0f74a4');
    // must not label/route back to the PUBLIC historical (agent_id-keyed) maker proof card
    expect(screen.queryByRole('link', { name: /maker proof card/i })).toBeNull();
  });

  it('renders the CURATED fixture label in the identity strip when the instance loader returns one', async () => {
    render(<GuardAblationScreen
      instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4"
      loadAblation={vi.fn().mockResolvedValue(divergentView())}
      loadInstance={vi.fn().mockResolvedValue(labeledInstance())}
    />);
    expect(await screen.findByTestId('ablation-fixture-label')).toHaveTextContent('France v Morocco · Home win');
  });

  it('omits the label (never blocks the panel) when the instance loader throws', async () => {
    render(<GuardAblationScreen
      instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4"
      loadAblation={vi.fn().mockResolvedValue(divergentView())}
      loadInstance={vi.fn().mockRejectedValue(new Error('403 not yours'))}
    />);
    // The panel still renders in full…
    expect(await screen.findByTestId('ablation-timeline')).toBeInTheDocument();
    // …and the label is simply absent, never a fabricated matchup.
    await waitFor(() => expect(screen.getByTestId('ablation-identity')).toBeInTheDocument());
    expect(screen.queryByTestId('ablation-fixture-label')).toBeNull();
  });

  it('omits the label when the instance carries no CURATED label', async () => {
    render(<GuardAblationScreen
      instanceId="mm-inst-0f74a4" backHref="/proof/maker/mm-inst-0f74a4"
      loadAblation={vi.fn().mockResolvedValue(divergentView())}
      loadInstance={vi.fn().mockResolvedValue(labeledInstance({ fixture_label: null, market_label: null }))}
    />);
    expect(await screen.findByTestId('ablation-timeline')).toBeInTheDocument();
    expect(screen.queryByTestId('ablation-fixture-label')).toBeNull();
  });
});

// Route-level identity-domain wiring (Codex gate): the ablation route is INSTANCE-keyed
// (GET /maker/live-ab/{instanceId}), so its page must send the same instance_id back to the
// deployed-instance page — NEVER to the public, agent_id-keyed /proof/maker/{id} historical card
// (which would render unrelated MM-R1 evidence under this instance id).
describe('GuardAblationPage route wiring (instance identity domain)', () => {
  it('passes an /instances/{instance_id} backHref (instance domain), not /proof/maker/{id}', async () => {
    const el = await GuardAblationPage({ params: Promise.resolve({ instanceId: 'inst_42086128' }) });
    expect(el.type).toBe(GuardAblationScreen);
    expect(el.props.instanceId).toBe('inst_42086128');
    expect(el.props.backHref).toBe('/instances/inst_42086128');
    expect(el.props.backHref).not.toContain('/proof/maker/');
  });
});
