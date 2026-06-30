'use client';
import { useState } from 'react';

export interface AgentOpsState {
  isOpen: boolean;
  agentId: string | null;
  open: (id: string) => void;
  close: () => void;
}

export function useAgentOps(): AgentOpsState {
  const [agentId, setAgentId] = useState<string | null>(null);
  return {
    isOpen: agentId !== null,
    agentId,
    open: (id: string) => setAgentId(id),
    close: () => setAgentId(null),
  };
}
