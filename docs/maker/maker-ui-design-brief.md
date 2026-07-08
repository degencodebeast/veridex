# Maker Arena UI — Design Brief (MM-R1)

**For:** the designer pass (Claude-design / `designer`) that builds the maker UI.
**Prereqs done:** backend bridge is live — `GET /maker/arena-result` (`veridex/api/maker_router.py`), frozen fixture `contracts/fixtures/maker_arena_result.json`, typed wire binding (`apps/web/lib/{wire,contracts,api,mock}.ts`, `getMakerArenaResult()`).
**Design around the REAL fields + REAL components below.** This lane must be *indistinguishable in polish* from the existing CLV screens — reuse the app's primitives, don't invent new ones.

## What this lane is (one paragraph)
The MarketMaker arena scores a maker agent on an evidence ladder, **structurally separate from the directional CLV leaderboard (SEC-005)**. At MM-R1 it measures forward-markout quote quality vs future TxLINE fair value and runs a toxicity-based falsification (a **naive control** vs a **TxLINE-fair candidate**). It makes **no fill/PnL/executable-edge claim** (`real_executable_edge_bps` is always `null`). The UI must express that honesty and must NOT look like or merge into the CLV board.

## The frozen data contract (design to THESE fields)
`getMakerArenaResult()` → envelope:
- `lane: "maker"`, `rank_axis: "avg_toxicity_loss_bps"`, `rank_axis_direction: "asc"` (lower = better).
- `result.maker_leaderboard[]`: `agent_id`, `maker_rank`, **`avg_toxicity_loss_bps` (THE RANK AXIS, lower is better)**, `avg_markout_bps` (**DIAGNOSTIC ONLY**), `abstained`, `quote_count`, `scored`, `real_executable_edge_bps: null`.
- `result.falsification`: `verdict` (`SEPARATED` | `INCONCLUSIVE` | `INVERTED`), `delta_bps`, `ci_low_bps`, `ci_high_bps`, `headline`.
- `result.window_clv_analog`: `note`, `window_markout_bps`, `window_action_count` (labeled maker-markout aggregate — **not** CLV).
- `result`: `rung` (`"MM-R1"`), `fixture_universe_n` (`18`), `small_n_flag` (`true`), `real_executable_edge_bps: null`.
- `proof_card`: `rung`, `uncalibrated`, `headline`, `falsification`, `n_fixtures`, `small_n_note`, `trades_not_fills_caveat`.
- `diagnostics`: `avg_markout_bps_label="diagnostic_not_rank_axis"`, `avg_toxicity_loss_bps_label="rank_axis_lower_is_better"`, `real_executable_edge_bps_label="always_null_no_fill_or_pnl_claim"`.
Real sealed values today: `txline-fair-mm` rank 1 (toxicity 129) beats `naive-mm` rank 2 (toxicity 172); falsification `SEPARATED`, Δ+43 bps, CI [34,52].

## Navigation & placement (grounded in `lib/nav.ts`)
Your IA is fixed: `NAV_SECTIONS` = 5 top-level tabs (Competitions, Arena, Markets, Leaderboard, Agents); `CONTEXTUAL_ROUTES` are deep-link/dropdown only — *"NEVER top-level tabs (REQ-002/003)"* — and the existing **Proof Card lives there** (`/proof/...`). So:
- **Do NOT add a 6th top-level "Maker" tab.**
- **Maker Leaderboard = a lane INSIDE the existing Leaderboard screen**, switched by a `SegmentedControl` (`Directional | Maker`). This mirrors what `LeaderboardScreen.tsx` already does (it already renders a `SegmentedControl` for the `ALL/REPLAY/LIVE` source filter).
- The **lane switch sits ABOVE the source filter** — it is a *higher* hierarchy level (different measurement, not a filter on the same rows). Do not make it a peer of `ALL/REPLAY/LIVE`.
- **Maker Proof Card = a deep-link route** (e.g. `/proof/maker/[id]` or `/maker/proof/[id]`) added to `CONTEXTUAL_ROUTES`, mirroring the existing `/proof/...` — **not** a tab.
- URL-addressable lane (`/leaderboard?lane=maker` or similar) so it's shareable (deep-linking); switching lanes preserves each lane's own filter/scroll state.

## Screen 1 — Maker Leaderboard (a lane within LeaderboardScreen)
- Selecting the **Maker** lane swaps the ENTIRE table + banner (different columns, rank label, data source). It is not a filtered view of the CLV rows.
- **Banner copy per lane** (reuse the existing banner slot): Directional keeps *"Rank is Avg CLV only…"*; Maker shows *"Rank is adverse-selection toxicity (lower = better) — a separate lane, not CLV. Mean markout is a diagnostic, not the rank axis. Two different lanes, different agents."*
- **Columns** (all numeric via `Num`): `#` (maker_rank), `AGENT`, `TOXICITY LOSS` (primary, lower=better), `MARKOUT ⓘ` (labeled diagnostic — secondary, muted), `QUOTES` (quote_count), `ABSTAINED`, and a `RUNG` badge (MM-R1).
- **Headline strip above the table = the falsification result** (the actual claim), e.g. a `SEPARATED` badge + *"TxLINE-fair separated from naive control: Δ+43 bps quote quality, CI [34,52]"*. Lead with this, not the mean.
- A `small-n` badge/note: *"n=18 (Polymarket-resolved cp1) · small sample."*

## Screen 2 — Maker Proof Card (deep-link route; mirror `ProofCardScreen` structure, reduced)
Mirror the header + 2-column labeled-section layout of `ProofCardScreen.tsx`, but a **reduced** card (no Merkle forest / VerifyButton unless/until the maker result gets a verify path — use honest-empty for those):
- **Header:** "Maker Proof Card" + a `RUNG` badge (MM-R1) + `config_hash` (via `shortHash`) + an `InfoTip`.
- **Lead section = the falsification claim:** verdict badge + `Δ delta_bps` + `CI [ci_low, ci_high]` (all `Num`/mono), headline text.
- **Universe:** `n_fixtures=18` + the `small_n_note` verbatim.
- **`window_clv_analog`:** render its `note` verbatim (labeled maker-markout analog, **not** CLV) + the two numbers.
- **Caveats block:** the `trades_not_fills_caveat`, and an explicit *"No executable-edge / PnL / fill claim — `real_executable_edge_bps` is null by construction."*
- **`UNCALIBRATED` badge** ONLY when `proof_card.uncalibrated` / `result.r2_bracket` is present (absent at MM-R1 — so show nothing, not a false badge).

## Component inventory — reuse these REAL primitives (do not invent)
- **Lane switch:** `SegmentedControl<'directional'|'maker'>` (`@/components/ui/SegmentedControl`) — `ariaLabel="Leaderboard lane"`, options `[{value:'directional',label:'Directional'},{value:'maker',label:'Maker'}]`. (Its `locked` option support can gate future rungs later.)
- **Numbers:** `Num` (`@/components/ui/Num`), `kind="bps"` for toxicity loss / markout / delta / CI bounds — gives tabular figures so columns don't shift. Use `.mono` for hashes/counts.
- **Chips:** `Badge` (`@/components/ui/Badge`) — **add new variants to `lib/badges.ts` `BADGE_META`** (glyph + label): `mm-r1`, `separated`, `inconclusive`, `inverted`, `uncalibrated`, `small-n`, `trades-not-fills`. Do NOT hand-roll ad-hoc chip spans.
- **Tooltips:** `InfoTip` (`@/components/ui/InfoTip`) — **add new `GLOSSARY` entries** (`@/lib/glossary`): `toxicity_loss` ("mean of per-quote adverse-selection loss; lower is better"), `mean_markout_diagnostic` ("raw two-sided mean ≈ half_spread/ref — geometry, not quality; not the rank axis"), `falsification` ("pairwise bootstrap; SEPARATED = whole CI above zero"), `maker_small_n` ("n=18 Polymarket-resolved cp1 fixtures").
- **Data source:** `getMakerArenaResult()` (`@/lib/api`) — mock mode already reads the fixture through `adaptMakerArenaResult`. **Never** call `getLeaderboard`/`rankByAvgClv`.
- **Styling:** CSS modules (`*.module.css`) + the app's semantic tokens, like every other screen. No raw hex, no ad-hoc Tailwind color values. Design light + dark together (the app is dark-themed).

## Honest-empty states (reuse the app's idiom)
`ProofCardScreen` already models the voice — *"root forest computed internally; not yet surfaced by the API"*, *"empty · no records"*. Reuse it, never a blank panel:
- No sealed artifact → *"Maker arena not yet run."*
- R1.5 trade-aware → *"Trade-aware diagnostic: future · operator-gated (real on-chain trade capture not run)."*
- R2 bracket → *"Fill-assumption bracket: not present at MM-R1."*
- Anchor / verify → *"not yet surfaced by the API"* (until wired).

## Copy & honesty rules (non-negotiable — must be VISIBLE)
- Rank axis is **toxicity, not CLV** — label it on the board and in the tooltip.
- **Mean markout is a diagnostic**, never the ranking or the headline (a naive maker can have a *higher* mean yet be *more* toxic — that is literally the current data: naive markout 1060 but toxicity 172 → rank 2).
- **No executable-edge / PnL / fill claim** anywhere — `real_executable_edge_bps` is always null.
- **Two lanes, different agents.** The lane switch is NOT "the same agents by maker performance" — the maker lane's agents (`naive-mm`, `txline-fair-mm`) are a *different population* from your directional agents. Copy must never imply a re-ranking of one set.
- **Trades are not our fills** (surface the R1.5 caveat).
- **Small n=18** → always show the small-sample caveat.
- Lead with the **falsification verdict + CI**, not the leaderboard mean.

## Accessibility (§1 — must hold)
- Verdict / rung / caveats via `Badge` (**glyph + text**), never color alone (`color-not-only`). The CI/verdict must be readable as text, not just a colored bar.
- Lane switch keeps `SegmentedControl`'s built-in `radiogroup` semantics + a clear `ariaLabel`.
- Proper heading hierarchy (the screen's `h1` is "Leaderboard"; maker sections use sequential headings). Tabular `Num` for all data columns.

## Do NOT build yet / do NOT reuse
- No R1.5 "trade-aware" panel with data (operator-gated) — honest-empty only. No R2 UI beyond the conditional `UNCALIBRATED` badge. No R3/R4.
- **SEC-005 code boundary (unchanged):** do NOT reuse `LeaderboardRow`/`LeaderboardResponse`/`ProofArtifact`, `rankByAvgClv`, `adaptLeaderboard`, or `adaptProofArtifact`. Use the `Maker*` types + `getMakerArenaResult()`. The toggle is a UI shell over **two fully separate render paths + data sources**.

## Deliverable
1. Maker lane inside `LeaderboardScreen` (segmented `Directional | Maker`, full table + banner swap) + a `MakerProofCard` deep-link route/screen.
2. New `Badge` variants + `GLOSSARY` entries listed above.
3. Component inventory + exact copy (rank-axis label, diagnostic-markout label, falsification result, caveats) as implemented.
4. Wired to `getMakerArenaResult()` (mock reads the fixture). Vitest coverage; a Playwright smoke if the repo has e2e.
5. Frontend-builder notes: which CLV components/types must NOT be reused (above), and where the lane switch sits relative to the source filter.
