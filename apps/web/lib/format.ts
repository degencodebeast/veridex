// Pure display helpers. Numeric color = sign of value (GUD-001); decimal odds
// per REQ-042 (TxLINE Prices are integers = decimal x1000).
export type SignClass = 'pos' | 'neg' | 'zero';

export function signClass(n: number): SignClass {
  if (n > 0) return 'pos';
  if (n < 0) return 'neg';
  return 'zero';
}

export function fmtBps(n: number): string {
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)} bps`;
}

export function fmtDecimalOdds(milli: number): string {
  return (milli / 1000).toFixed(3);
}

export function fmtPct(implied: string): string {
  return `${Number(implied).toFixed(1)}%`;
}

export function shortHash(h: string): string {
  if (h.length <= 12) return h;
  return `${h.slice(0, 6)}…${h.slice(-4)}`;
}
