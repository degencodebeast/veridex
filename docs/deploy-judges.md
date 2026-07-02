# Judge quickstart — run the demo, verify the proof

This is the exact path from a **clean machine** to a **verifiable proof**, in about two minutes.
Everything below runs **offline and deterministic** by default: no live network, no real-money
orders, no wallet, no secrets. You will run one command, then open a URL that **recomputes** the
result from sealed evidence — you verify the winner instead of trusting a screenshot.

What you will see:

- The flagship agent **Sharp Momentum v2** replayed through the *same* incremental core the live
  loop uses, scored on **closing-line value (CLV)** into an honest report.
- A `demo_manifest.json` listing each sealed run's `run_id` and its `/runs/{run_id}/verify` URL.
- A **Verify** endpoint that re-runs the deterministic law over the sealed event log and returns a
  per-check verdict — tamper with one sealed byte and the proof goes red.

Honest by construction: the offline demo is labelled **Backtest** over **banked** odds. It is
**not** a live run, **not** real money, and **not** a fabricated result. The default pack ships
*synthetic illustrative* odds (see the last section); the run over them is a genuine sealed proof.

---

## 1. Prerequisites

- **Python 3.11+** (`python --version`).
- **git**, to clone the repo.

No database, no wallet, no API keys, and no internet access are required for the default demo.

## 2. Install

From the repository root (`veridex-arena/`):

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[api]"                    # base engine + the read/verify API (FastAPI + uvicorn)
```

> The offline demo itself needs only the base install; the `[api]` extra adds the small
> FastAPI/uvicorn server used in step 4 so the verify URLs resolve locally.
>
> If you prefer `uv` (the repo ships a `uv.lock`): `uv sync --extra api`, then prefix the commands
> below with `uv run` instead of activating the venv.

## 3. Run the demo (offline)

```bash
python scripts/demo_phase2d.py
```

This replays the banked ReplayPack that ships with the repo, produces two **real sealed runs**, and
writes `demo_manifest.json` to the repo root. It prints a summary like:

```
=== Veridex Phase-2D demo ===
flagship strategy : Sharp Momentum v2
pack              : demo_pack  (content_hash cd7e0daa53a0…)
manifest          : demo_manifest.json
mode labels are HONEST — a Backtest over BANKED odds, never 'Live'; no real-money orders.

  [backtest] Backtest  run_id=bt_cd7e0daa53a0_wc_demo
             avg_clv=626.5 bps  sample=35  (high)
             verify → /runs/bt_cd7e0daa53a0_wc_demo/verify
  [paper   ] Backtest  run_id=paper_cd7e0daa53a0
             verify → /runs/paper_cd7e0daa53a0/verify
```

- **`backtest`** — the flagship Sharp Momentum v2 scored against the pack's reconstructed pre-match
  close. `avg_clv` is the mean closing-line value in basis points; `sample` is the number of scored
  decisions; the confidence tier reflects the sample size.
- **`paper`** — the same flagship through the standalone **paper** lane: **proof-only, no venue
  orders**. On a replay source this too honestly reads *Backtest* — the `kind` names the lane, the
  mode label names the source × execution honesty.

The run ids are **deterministic** (pinned from the pack's content hash), so re-running produces the
same ids and the same numbers.

## 4. Verify the proof

Serve the read API against the same runs and open the printed URLs:

```bash
python scripts/demo_phase2d.py --serve        # boots the API on http://localhost:8080
```

The summary now prints fully-qualified URLs, e.g.
`http://localhost:8080/runs/bt_cd7e0daa53a0_wc_demo/verify`. Open one (or `POST` it) to see the
**recompute-from-sealed-evidence** verdict:

```bash
curl -X POST http://localhost:8080/runs/bt_cd7e0daa53a0_wc_demo/verify
```

The response re-derives the score root, recomputes the sealed evidence hash, and rebuilds the run
manifest — so `verified: true` means the numbers were **independently reproduced from the sealed
event log**, not asserted. This is the same `/runs/{id}/verify` path an arena run uses; the frontend
never re-implements the law, it renders this verdict.

Press `Ctrl-C` to stop the server.

## 5. What honesty looks like here

- **Mode labels never overclaim.** A replay/paper run is a **Backtest** — never "Live", never
  "Dry Run", never a real-money order. The label ladder is total: an unmapped source × execution
  pair raises rather than mislabelling.
- **`real_executable_edge_bps` is `null`** on the paper/replay path — there is no live venue fill to
  claim, so the report says so explicitly instead of implying one.
- **The proof is the product.** `verified: true` is a recomputation, not a checkmark image.

## 6. Running against a real captured pack (operator, optional)

The default `demo_pack` contains **synthetic illustrative** odds — chosen to exhibit the sharp,
sustained repricing Sharp Momentum v2 is built to catch. The run over it is a genuine sealed proof,
but the *odds themselves* are illustrative, not captured market history.

To produce a **real-odds** artifact, point the demo at a real captured ReplayPack (recorded live,
then converted with the recorder/pack tooling — a read-only odds capture, no orders):

```bash
python scripts/demo_phase2d.py --pack /path/to/captured_pack --fixture-id <FIXTURE_ID> --serve
```

Everything else is identical: real sealed runs, honest labels, verifiable URLs.
