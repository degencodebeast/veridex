import type { Metadata } from 'next';
import { WhyVeridexScreen } from '@/components/screens/marketing/WhyVeridexScreen';

// Public explainer route — lives OUTSIDE the (app) group, so it renders standalone marketing
// chrome (its own nav) with no AppShell / left rail. Reached from the landing nav "Why Veridex".
export const metadata: Metadata = {
  title: 'Why Veridex — Veridex',
  description: 'Trading agents need an independent scoreboard. Intelligence proposes, policy controls, the deterministic law verifies and scores.',
};

export default function WhyVeridexPage() {
  return <WhyVeridexScreen />;
}
