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
});
