import type { Metadata } from 'next';
import { HowItWorksScreen } from '@/components/screens/marketing/HowItWorksScreen';

// Public explainer route — lives OUTSIDE the (app) group, so it renders standalone marketing
// chrome (its own nav) with no AppShell / left rail. Reached from the landing nav "How it works".
export const metadata: Metadata = {
  title: 'How It Works — Veridex',
  description: 'From agent decision to proof you can inspect: the six-stage proof rail — evidence, law, policy, receipt, score, anchor.',
};

export default function HowItWorksPage() {
  return <HowItWorksScreen />;
}
