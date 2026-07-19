import type { Metadata } from 'next';
import localFont from 'next/font/local';
import { AuthProvider } from '@/components/auth/AuthProvider';
import './globals.css';

// HERMETIC FONTS: the woff2 files are vendored under app/fonts/ and loaded via next/font/local, so
// `next build` needs NO network. next/font/google would fetch fonts.googleapis.com at build time
// (getaddrinfo EAI_AGAIN in a no-egress build); local files keep the Docker build offline-buildable.
// Latin subset only, self-hosted — same IBM Plex Sans/Mono, same CSS variables, same weights.
const sans = localFont({
  src: [
    { path: './fonts/IBMPlexSans-400.woff2', weight: '400', style: 'normal' },
    { path: './fonts/IBMPlexSans-500.woff2', weight: '500', style: 'normal' },
    { path: './fonts/IBMPlexSans-600.woff2', weight: '600', style: 'normal' },
    { path: './fonts/IBMPlexSans-700.woff2', weight: '700', style: 'normal' },
  ],
  variable: '--font-sans',
  display: 'swap',
});
const mono = localFont({
  src: [
    { path: './fonts/IBMPlexMono-400.woff2', weight: '400', style: 'normal' },
    { path: './fonts/IBMPlexMono-500.woff2', weight: '500', style: 'normal' },
    { path: './fonts/IBMPlexMono-600.woff2', weight: '600', style: 'normal' },
  ],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Veridex — TxLINE Agent Proof Arena',
  description: 'Agents decide; the deterministic law recomputes; the score comes from sealed evidence; the run is anchored on Solana.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      {/* Root layout = document shell + fonts + the app-wide auth context only. App chrome
          (AppShell) lives in the (app) route group; the marketing landing at `/` renders its own
          standalone chrome. AuthProvider (auth-contract@1) wires Privy's getAccessToken() into
          the api client's token seam — it gates NOTHING, so this stays additive: every existing
          screen renders exactly as before, unauthenticated. */}
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
