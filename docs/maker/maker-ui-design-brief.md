# Maker Arena UI — Design Brief (MM-R1)

**For:** the designer pass (Claude-design / `designer` agent) that builds the maker UI.
**Prereqs already done:** the backend bridge is live — `GET /maker/arena-result` (`veridex/api/maker_router.py`), frozen fixture `contracts/fixtures/maker_arena_result.json`, and typed wire binding (`apps/web/lib/{wire,contracts,api,mock}.ts`, `getMakerArenaResult()`). **Design around the REAL fields below, not prose.**

## What this lane is (one paragraph)
The MarketMaker arena scores a maker agent on an evidence ladder, **structurally separate from the directional CLV leaderboard (SEC-005)**. At MM-R1 it measures forward-markout quote quality vs future TxLINE fair value and runs a toxicity-based falsification (a naive control vs a TxLINE-fair candidate). It makes **no fill/PnL/executable-edge claim** (`real_executable_edge_bps` is always `null`). This UI must express that honesty, and must NOT look like or merge into the CLV board.

## The frozen data contract (design to THESE fields)
`GET /maker/arena-result` → envelope:
- `lane: "maker"`, `rank_axis: "avg_toxicity_loss_bps"`, `rank_axis_direction: "asc"` (lower = better).
- `result.maker_leaderboard[]`: `agent_id`, `maker_rank`, **`avg_toxicity_loss_bps` (THE RANK AXIS, lower is better)**, `avg_markout_bps` (**DIAGNOSTIC ONLY**), `abstained`, `quote_count`, `scored`, `real_executable_edge_bps: null`.
- `result.falsification`: `verdict` (`SEPARATED` | `INCONCLUSIVE` | `INVERTED`), `delta_bps`, `ci_low_bps`, `ci_high_bps`, `headline` (e.g. `SEPARATED_QUOTE_QUALITY`).
- `result.window_clv_analog`: `note`, `window_markout_bps`, `window_action_count` (a labeled maker-markout aggregate — **not** directional CLV).
- `result`: `rung` (`"MM-R1"`), `fixture_universe_n` (`18`), `small_n_flag` (`true`), `real_executable_edge_bps: null`.
- `proof_card`: `rung`, `uncalibrated`, `headline`, `falsification`, `n_fixtures`, `small_n_note`, `trades_not_fills_caveat`.
- `diagnostics`: `avg_markout_bps_label="diagnostic_not_rank_axis"`, `avg_toxicity_loss_bps_label="rank_axis_lower_is_better"`, `real_executable_edge_bps_label="always_null_no_fill_or_pnl_claim"`.
Real sealed values today: `txline-fair-mm` rank 1 (toxicity 129) > `naive-mm` rank 2 (toxicity 172); falsification `SEPARATED`, Δ+43 bps, CI [34,52].

## Design these (mirror the existing directional screens, kept separate)
1. **Maker Leaderboard screen** (parallel to `components/screens/LeaderboardScreen.tsx`; own `MakerLeaderboardRow`, NOT `rankByAvgClv`):
   - Ranked by `avg_toxicity_loss_bps` ascending. Primary metric column = **toxicity loss (lower = better, "adversely selected less")**.
   - `avg_markout_bps` shown only as a **secondary column explicitly labeled "raw mean markout — diagnostic, not rank axis"** (it's spread/reference geometry, not quality). Never headline it.
   - A headline banner = the **falsification verdict + CI** (the actual claim), e.g. "TxLINE-fair SEPARATED from naive control: +43 bps quote-quality, CI [34,52]".
   - Show `quote_count`, `abstained`, and an `n=18 · small sample` caveat chip.
2. **Maker Proof Card screen** (parallel to `components/screens/proof/ProofCardScreen.tsx`; render from `proof_card` + `result`):
   - Lead with the **falsification verdict + CI** as the claim; then `rung` (MM-R1), `n_fixtures=18` + small-n note.
   - `window_clv_analog` rendered with its `note` verbatim (labeled maker-markout analog, **not** CLV).
   - The **trades-not-fills caveat** and a clear **"no executable-edge / PnL / fill claim" statement** (because `real_executable_edge_bps` is null everywhere).
   - An `UNCALIBRATED` badge ONLY when an R2 bracket is present (`proof_card.uncalibrated` / `result.r2_bracket`) — absent at MM-R1.
3. **Nav:** a separate **"Maker" tab** in `lib/nav.ts` (a distinct surface expresses the SEC-005 lane isolation; do not add maker rows to the CLV nav/board).

## Honesty rules that MUST be visible (non-negotiable)
- The rank axis is **toxicity, not CLV** — label it on the board.
- **Mean markout is a diagnostic**, never the ranking or the headline (a wide/naive maker can have a *higher* mean yet be *more* toxic — that's literally the current data).
- **No executable-edge / PnL / fill claim** anywhere — `real_executable_edge_bps` is always null.
- **Trades are not our fills** (surface the R1.5 caveat).
- **Small n=18** → always show the small-sample caveat.
- Lead with the **falsification verdict** (the pairwise statistical claim), not the leaderboard mean.

## Do NOT build yet (out of scope for this pass)
- No R1.5 "trade-aware" panel until real on-chain trade capture exists (operator-gated, not run). Show it as unavailable if referenced at all.
- No R2 bracket UI beyond the `UNCALIBRATED` badge (absent in the MM-R1 artifact).
- No R3/R4 anything (future-only).
- Do NOT reuse `LeaderboardRow`/`LeaderboardResponse`/`ProofArtifact` or route through `adaptLeaderboard`/`adaptProofArtifact`. Use the `Maker*` types + `getMakerArenaResult()` (mock mode reads the fixture).

## Deliverable
`MakerLeaderboardScreen` + `MakerProofCardScreen` + a `Maker` nav entry + thin route files, wired to `getMakerArenaResult()`. Vitest coverage; a Playwright smoke if the repo has e2e. Match the existing app's visual language (don't clone the CLV board's copy — this lane's story is adverse-selection/toxicity, not CLV).
