# TxLINE API & Docs — Feedback from building Veridex

*Draft — factual findings from integrating TxLINE StablePrice odds + scores into a live sports-agent
proof arena. Everything below was observed empirically against the dev egress with a guest token, or
against the published OpenAPI. Offered as constructive feedback; the API was a pleasure to build on.*

## What worked well (so it's on the record)

- **Guest-JWT + `X-Api-Token` auth is clean.** `/auth/guest/start` mints a JWT that auto-refreshes;
  paired with the `X-Api-Token` header it just worked, first try, no manual token juggling.
- **Deep historical odds are a genuine asset.** A single `GET /api/odds/updates/{fixtureId}` returned
  the full pre-match→in-play movement history for a finished fixture (tens of thousands of updates
  spanning several days) — enough to reconstruct a closing line and replay a whole match offline. This
  is the single most valuable thing for backtesting/agent evaluation.
- **The Merkle validation endpoint** (`/api/odds/validation`) and the Solana-anchored "Historical
  Replay" framing are exactly the kind of provable-data primitive that composes well with downstream
  proof systems.

## Suggested doc / API clarifications

### 1. `/api/odds/updates/{fixtureId}` is documented as a 5-minute cache, but serves full history
The OpenAPI describes this endpoint as the "current in-memory 5-minute cache — NOT history." In
practice (dev egress) it returned the **entire fixture history** — many days of pre-match→in-play
updates in one call, not a 5-minute window. This is a *pleasant* surprise, but the doc **understates**
the endpoint, which matters two ways:
- Integrators who need closing-line reconstruction may wrongly conclude they must iterate the
  per-interval bucket endpoints (`/api/odds/updates/{epochDay}/{hour}/{interval}`) when a single call
  suffices.
- If the 5-minute-cache behavior is the *intended contract* and the full-history response is
  environment-specific (dev vs prod) or subject to change, that's a correctness risk for anyone who
  builds on the observed behavior. **Please document the actual retention/window guarantee per
  environment**, and whether the full-history response is stable to rely on.

### 2. Host/token scoping is ambiguous
The same guest token authenticated successfully against the **dev** host (`txline-dev.txodds.com`) but
was rejected by the production host with `"API Token not found"`. It would help to state explicitly, in
the auth/subscription docs, **which host a guest/free-tier token targets**, and how one obtains a token
scoped to production.

### 3. `CompetitionId` values in the subscription/tier docs don't match the fixture payloads
The subscription-tier doc references competition IDs (e.g. `1`, `12`) that appear to be *service-level*
identifiers, whereas the fixture payloads carry the actual competition IDs (observed: **World Cup =
`72`**, **International Friendlies = `430`**). A short mapping table — "free tier covers these
`Competition(Id)` values as they appear in the fixture feed" — would remove the ambiguity for anyone
filtering fixtures by competition.

### 4. Publish a canonical `SuperOddsType` reference table
The market/odds payloads key market families by a `SuperOddsType` string. The real values were not
obvious from the docs and differ from common shorthand — e.g. **totals are
`OVERUNDER_PARTICIPANT_GOALS`** (not `O/U`, `OU`, or `OVER_UNDER`), and match result is
`1X2_PARTICIPANT_RESULT`. An integrator mapping to an external venue *will* silently drop the totals
market if they assume `OU`. A published enum of `SuperOddsType` values (with a one-line description and
the outcome shape of each) would prevent that class of bug outright.

### 5. Document the near-certain-line suspension behavior (important for closing-line / CLV)
For near-certain markets (e.g. Over/Under 0.5 — "≥1 goal"), the book **suspends pricing before the
close**: a point-in-time snapshot at kickoff (`/api/odds/snapshot/{fixtureId}?asOf=<kickoff_ms>`)
returned `suspended=true` with an empty `stable_prob_bps` for that line — i.e. **no priced closing
value**. This is reasonable book behavior, but it has a real consequence for anyone computing
closing-line value: **some lines legitimately have no valid close**, and a correct integration must
handle that (decline to score / mark unavailable) rather than impute a last-known price. Two asks:
- Document *which* market types / probability ranges are expected to suspend near the close.
- Consider a first-class field distinguishing "suspended / no valid close" from "missing data," so
  integrators can tell an honest no-close apart from a gap.

## Net
The data quality and historical depth are excellent. The gaps are almost entirely **documentation**:
the true window/retention of the updates endpoint, host/token scoping, the competition-ID mapping, a
canonical `SuperOddsType` enum, and the suspension semantics for near-certain lines. Closing those
would make first-integration correctness much easier to get right.
