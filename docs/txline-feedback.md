# TxLINE API & Docs — Feedback from building Veridex

*Draft — factual findings from integrating TxLINE StablePrice odds + scores into a live sports-agent
proof arena. Everything below was observed empirically against the dev egress, found in the published
OpenAPI/docs, or clarified by TxLINE staff. Community reports are identified as such and are not treated
as API contracts without reproduction. Offered as constructive feedback; the API was a pleasure to
build on.*

## What worked well (so it's on the record)

- **Guest-JWT + `X-Api-Token` worked cleanly in our initial dev session.** Once acquired, the JWT and
  API token paired cleanly on data requests. This is an empirical observation about that integration
  session, not a claim that every currently published activation example or request/response schema is
  interchangeable across API versions and hosts.
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

### 6. Publish one explicit score-history endpoint matrix
This was our most consequential integration mistake. We correctly observed that
`/api/odds/updates/{fixtureId}` served deep history despite its 5-minute-cache description, then
incorrectly generalized that empirical behavior to `/api/scores/updates/{fixtureId}`. The score route
is documented as the current 5-minute interval; complete post-match score history belongs at
`/api/scores/historical/{fixtureId}`, while the day/hour/interval route is the bounded historical
acquisition path. Our client consequently described the score-updates route as full history, and our
backfill could preserve an empty or truncated score sibling while successfully building an odds-backed
pack. That unsupported generalization was our bug, not an API promise.

The asymmetry is nevertheless easy to miss. Please publish one table for fixtures, odds, and scores that
states, per endpoint:

- snapshot versus incremental updates versus bounded buckets versus full history;
- retention/window guarantees and whether the fixture must be completed;
- response ordering, sequence-continuity, pagination, and completeness guarantees;
- environment-specific differences and the response when data has expired.

In particular, please state whether bucketed score acquisition can recover records older than the
`/scores/historical/{fixtureId}` rolling window. A staff suggestion that chunks may still work is useful
direction, but not yet a stable contract on which a verifier should rely.

### 7. Document the soccer event lifecycle as a state machine
The score feed is richer than a stream of immutable facts. A goal or card may be enriched later by
`action_amend`; a VAR review has a separate `var` start and confirmed `var_end` resolution; and the
terminal VAR outcome is `Stands` or `Overturned`. TxLINE staff also clarified that a goal can sometimes
be marked `Confirmed=true` before a VAR review subsequently begins. That means confirmation alone is not
always terminal truth.

Please document a normative lifecycle covering at least `goal`/card, `action_amend`, `var`, `var_end`,
`action_discarded`, score restatements, and `game_finalised`. It should define stable event identity,
which records enrich an earlier event, which retract it, and how consumers should resolve conflicting
or out-of-order lifecycle records. This is correctness-critical for event studies and automated agents,
not merely display metadata.

### 8. Separate terminal action, status, period, and transport semantics
A community report showed `StatusId=100` on an `Action="disconnected"` record while the match clock was
still running. We have not promoted that report to an API fact without a pinned reproduction, but it is
a useful adversarial case: consumers should not infer finality from one integer field if the action and
score lifecycle disagree.

Please document the authoritative terminal predicate and the relationship among `Action`, `StatusId`,
period, score totals, and stat proofs. Examples should bind settlement/finality to the appropriate
`game_finalised` record and proof rather than to a bare status sighting.

### 9. Publish fixture duplicate, cancellation, and supersession lineage
TxLINE staff confirmed that historical fixture discovery contains legacy duplicate fixture IDs and that
some cancelled twins could not be retroactively annotated with the newer `GameState=6` field. Without a
stable lineage signal, an integrator may double-count a fixture or silently collapse a genuine rematch.

Please expose or document a durable duplicate/supersession relationship, cancellation authority, and
whether fixture IDs may be replaced. Historical consumers should be able to distinguish `ACTIVE`,
`CANCELLED`, `DUPLICATE`, and genuinely ambiguous records without matching only on teams and start time.

### 10. Make the SSE resume contract prominent
The streams support fixture filtering and `Last-Event-ID`, but the operational contract deserves a
complete example. Please specify whether SSE `id:` values are global or fixture-scoped, how long replay
state is retained, whether reconnect may redeliver records, how an expired ID fails, and whether odds
and scores share identical semantics. A reconnect example should show persistence of the last accepted
upstream ID plus deterministic duplicate handling. Without that, it is easy to parse only `data:` lines
and accidentally discard the resume identity while believing the recorder is gap-safe.

### 11. Version and demonstrate proof verification end to end
HTTP 200 establishes transport success, not that a returned stat key/value/proof is authoritative. A
community report claims unsupported stat keys can return a zero value and a proof that does not reconcile
to `eventStatRoot`; we have not reproduced that report and therefore do not present it as a TxLINE defect.
It is still the correct negative test for an integration.

Please publish one end-to-end verifier that checks the returned value, proof path, expected root, account,
program ID, API version, and matching IDL before accepting evidence. Also publish an API/OpenAPI/IDL/program
compatibility matrix. During integration, V3 API documentation and the publicly available mainnet IDL did
not appear synchronized on `validate_stat_v3`; a verifier must not guess across that boundary.

The same versioned contract would help authentication. Our initial dev flow worked, but the Python helper
we wrote from an earlier shape now disagrees with current examples over activation request names and
response encoding. A canonical example should pin `txSig`/wallet-signature/league or service-level fields,
the API-token response format, JWT requirements, target host, and the exact version to which they apply.

## Net
The data quality, historical depth, and proof primitives are excellent. The integration friction was a
mixture of **our own unsupported generalization**, documentation ambiguity, legacy-data lineage, and
API/example/IDL version synchronization. The highest-impact improvement would be a versioned endpoint and
lifecycle matrix: exact history windows, event amendment/reversal semantics, fixture lineage, SSE resume,
authentication, and proof verification in one place. That would make first-integration correctness much
easier and would help consumers distinguish byte integrity from completeness and transport success from
verified evidence.
