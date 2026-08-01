import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentProfileScreen } from '@/components/screens/AgentProfileScreen';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

const profile = AGENT_PROFILES.value_clv;

describe('AgentProfileScreen (REQ-021)', () => {
  it('renders the config-pinned generated Strategy caption read-only (no editable affordance)', () => {
    render(<AgentProfileScreen profile={profile} />);
    const caption = screen.getByTestId('strategy-caption');
    expect(caption).toHaveTextContent(profile.strategy_caption);
    expect(within(caption).queryByRole('textbox')).toBeNull();
    expect(screen.getByText(new RegExp(profile.config_hash))).toBeInTheDocument();
  });

  // Cross-surface honesty: eligibility must come from the AUTHORITATIVE eligibility_badge, NOT be
  // re-derived from proof_mode. Re-deriving would contradict the Leaderboard for the same agent (a
  // proof_mode 'reproducible' unproven agent is not-eligible on the board but would derive eligible).
  it('renders the authoritative eligibility_badge, never one re-derived from proof_mode', () => {
    render(<AgentProfileScreen profile={{ ...profile, proof_mode: 'reproducible', eligibility_badge: 'not-eligible' }} />);
    expect(screen.getByText('Not Eligible')).toBeInTheDocument();
    expect(screen.queryByText('Eligible')).toBeNull(); // never the re-derived eligible badge
  });

  it('is CLV-native: shows Avg CLV + confidence, not ROI/trophies', () => {
    render(<AgentProfileScreen profile={profile} />);
    expect(screen.getByText(/avg clv/i)).toBeInTheDocument();
    expect(screen.getByText(/CONF/)).toBeInTheDocument();
    expect(screen.queryByText(/ROI|trophy/i)).toBeNull();
  });

  it('shows THIS agent\'s own record honestly — never implies cloned/inherited performance (#3)', () => {
    render(<AgentProfileScreen profile={profile} />);
    expect(screen.getByText(/never asserts performance/i)).toBeInTheDocument();
    expect(screen.queryByText(/inherit/i)).toBeNull();
  });

  it('links Clone to the clone preview carrying the source agent', () => {
    render(<AgentProfileScreen profile={profile} />);
    expect(screen.getByRole('link', { name: /clone this agent/i })).toHaveAttribute('href', '/clone?source=value_clv');
  });

  it('opens the runtime drawer from RUNTIME · LOGS', async () => {
    const user = userEvent.setup();
    const onOpenRuntime = vi.fn();
    render(<AgentProfileScreen profile={profile} onOpenRuntime={onOpenRuntime} />);
    await user.click(screen.getByRole('button', { name: /runtime · logs/i }));
    expect(onOpenRuntime).toHaveBeenCalledWith('value_clv');
  });

  it('links completed-competition rows to their proof', () => {
    render(<AgentProfileScreen profile={profile} />);
    expect(screen.getByRole('link', { name: /ESP v NED/i })).toHaveAttribute('href', '/proof/run_esp_ned_01');
  });

  // Quick honest enrichment: an off-mock REAL profile has runs>0 but no per-competition breakdown from
  // the leaner endpoints. Implying "No completed competitions yet." would be dishonest — the guard flips
  // the empty-state copy to an honest "not exposed" note when breakdown_available === false.
  it('empty competitions with breakdown_available:false shows the honest "not exposed" note, NOT "none yet"', () => {
    render(<AgentProfileScreen profile={{ ...profile, completed_competitions: [], breakdown_available: false }} />);
    expect(screen.getByText(/exposed on the public profile/i)).toBeInTheDocument();
    expect(screen.queryByText(/No completed competitions yet/i)).toBeNull();
  });

  it('empty competitions with the flag ABSENT keeps the original "none yet" copy (mock-path regression)', () => {
    render(<AgentProfileScreen profile={{ ...profile, completed_competitions: [] }} />);
    expect(screen.getByText(/No completed competitions yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/exposed on the public profile/i)).toBeNull();
  });
});
