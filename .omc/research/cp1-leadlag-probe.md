# cp1 lead-lag probe -- does the TxLINE FV lead the venue mid?

OFFLINE event-aligned SIGNED-RESPONSE lead-lag (committed ReplayPack FV + pinned venue frames; no network). Per `(fixture_id, venue_market_ref)` market the live series is compressed to non-overlapping venue-mid CHANGE events; at each event the signal is `sign((FV_t - prior_mid_t) - basis_t)` with an EXPANDING-MEDIAN basis over strictly prior events (no look-ahead). Two outcomes are scored against that one signal: **NEXT-change** (does the *next* venue move follow the signal -- the honest, forward-predictive headline) and **SAME-change** (does the *just-occurring* move follow it -- near-circular, contrast only). A **placebo** reads the residual AFTER the move and must be anti-predictive. Per-market, never pooled into one series.

## VERDICT: FV LEADS (modest, latency-driven)

> **FV LEADS the venue mid, modestly, on a latency/data-freshness basis.** The NEXT-change hit rate at the 50 bps gate is the sizing number; the far higher SAME-change rate is the near-circular contrast (it scores the residual against the very move being predicted) and is NOT the edge. This is a data-freshness edge on a **backfilled** venue series -- live-venue staleness is unconfirmed. It is non-circular on repo evidence (FV = TxLineStablePriceDemargined, read from TxLINE coordinates, disjoint from the Polymarket price frames) with the TxLINE upstream unprovable here.

## Pooled + fixture-level significance (per threshold)

| threshold_bps | NEXT hit | NEXT z | NEXT n | SAME hit | SAME z | SAME n | PLACEBO hit | PLACEBO z | PLACEBO n | #fix>0.5 | n_fix | fixture-level z |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 0.640 | 7.971 | 811 | 0.934 | 25.419 | 859 | 0.355 | -7.363 | 645 | 17 | 18 | 4.123 |
| 100 | 0.703 | 9.157 | 511 | 0.971 | 22.178 | 554 | 0.374 | -4.138 | 270 | 15 | 18 | 3.500 |
| 200 | 0.713 | 7.034 | 272 | 0.990 | 17.266 | 310 | 0.357 | -1.852 | 42 | 15 | 18 | 3.153 |

## Per-fixture NEXT-change hit rate (headline 50 bps gate)

| fixture_id | NEXT hit | > 0.5 |
|---|---|---|
| 17588229 | 0.841 | yes |
| 17588234 | 0.597 | yes |
| 17588245 | 0.509 | yes |
| 17588325 | 0.500 | no |
| 17588391 | 0.724 | yes |
| 17588404 | 0.651 | yes |
| 17926593 | 0.614 | yes |
| 18167317 | 0.825 | yes |
| 18172280 | 0.750 | yes |
| 18172469 | 0.667 | yes |
| 18175918 | 0.559 | yes |
| 18175981 | 0.571 | yes |
| 18175983 | 0.667 | yes |
| 18176123 | 0.667 | yes |
| 18179550 | 0.576 | yes |
| 18179551 | 0.628 | yes |
| 18179759 | 0.571 | yes |
| 18179763 | 0.579 | yes |

## Honest caveats

- **NEXT-change is the honest headline; SAME-change is near-circular.** SAME scores the residual against the very move being predicted, so it inflates far above the forward-predictive NEXT number. Only NEXT (and the anti-predictive placebo) establish a lead.
- **Data-freshness edge on a BACKFILLED venue series.** The venue mid is a slow step function reconstructed from pinned frames; the measured lead is the TxLINE FV moving ahead of the next venue refresh. Whether a LIVE venue is equally stale is UNCONFIRMED.
- **Non-circular on repo evidence, TxLINE upstream unprovable.** FV is the demargined TxLINE stable price (disjoint from the Polymarket frames), so the edge is not the venue predicting itself; but this repo cannot prove the TxLINE upstream is itself honest.
- **No look-ahead.** The basis at each event uses only strictly-prior events; the signal uses the PRE-move standing mid; the next-change outcome is a strictly later event.
