import type { Metadata } from 'next';
import { IBM_Plex_Sans, IBM_Plex_Mono } from 'next/font/google';
import './globals.css';

const sans = IBM_Plex_Sans({ subsets: ['latin'], weight: ['400', '500', '600', '700'], variable: '--font-sans' });
const mono = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500', '600'], variable: '--font-mono' });

export const metadata: Metadata = {
  title: 'Veridex — TxLINE Agent Proof Arena',
  description: 'Agents decide; the deterministic law recomputes; the score comes from sealed evidence; the run is anchored on Solana.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      {/* Root layout = document shell + fonts only. App chrome (AppShell) lives in the
          (app) route group; the marketing landing at `/` renders its own standalone chrome. */}
      <body>{children}</body>
    </html>
  );
}
