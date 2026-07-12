# MM-R1.5 Capture Runbook — Operator-Gated `OrderFilled` Capture

This runbook documents how an operator produces a pinned, provenance-bearing
`TradeArtifact` from Polymarket CTF Exchange V2 `OrderFilled` logs using
`veridex/maker/capture.py`. The capture path is **operator-gated**, **clean-room**,
and **network-isolated in tests**.

> Trades are **not** fills. A `NormalizedTradeRow` is a decoded trade between other
> venue participants — never a Veridex fill. No fill / PnL / edge / realized field
> exists anywhere in this path.

---

## 1. The `HYPERSYNC_API` operator secret

- `HYPERSYNC_API` is the operator's HyperSync access token. It is read from the
  environment **only** inside `capture_order_filled_artifact`, and **only to gate
  the run**.
- The token is **never** passed into `build_trade_artifact`, **never** written into
  the artifact / manifest / any log line, and **never** returned. The
  `TradeArtifact` manifest additionally rejects any secret-bearing key
  (`hypersync_api`, `api_key`, `bearer_token`, `authorization`, `secret`) by name.
- **Fail-closed:** with no injected client and `HYPERSYNC_API` unset,
  `capture_order_filled_artifact` raises `RuntimeError` **before** any network or
  file I/O. Never hardcode the token; never commit it; export it only in the
  operator shell:

  ```bash
  export HYPERSYNC_API="<operator-token>"   # operator shell only; never committed
  ```

---

## 2. GPL clean-room boundary

The decoder `decode_order_filled` was written **clean-room from the public CTF
Exchange V2 `OrderFilled` event ABI** (field names + 6-decimal USDC scaling). It
copies **no** code from any GPL-licensed reference implementation, imports nothing
from any untrusted reference tree, and executes nothing from one. `capture.py`
imports **only** the standard library and `veridex.*` — enforced by the source-scan
test `test_capture_module_imports_only_stdlib_and_veridex` in
`tests/test_maker_capture.py`.

Because `capture.py` performs **no** network import of its own, the network lives
entirely behind the injected `OrderFilledLogSource` client. The operator builds a
HyperSync-backed adapter in the operator harness (holding the token) and passes it
as `client=...`.

---

## 3. CTF Exchange V2 contract / genesis pins

| Pin | Value |
| --- | --- |
| Chain | Polygon (`chain_id = 137`) |
| Contract | CTF Exchange V2 (`OrderFilled` emitter) — `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Event signature | `OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)` |
| Collateral (USDC) leg | `assetId == "0"` |
| Amount decimals | 6 (USDC and CTF outcome tokens) |
| Reorg buffer | 20 confirmations |
| Records mapping hash | pinned `PINNED_MAPPING_HASH` (records-only) |

The `from_block` / `to_block` range is chosen by the operator to cover the CTF
Exchange V2 deployment (genesis) through the target capture window. Confirm the
contract address and genesis block against the deployment record before a
production capture.

---

## 4. Decode semantics

For each `OrderFilled` log the decoder derives:

- `price = usdc_leg / share_leg` — native probability in `[0, 1]`; the 6-decimal
  scale cancels in the ratio. A derived price outside `[0, 1]` raises `MarkoutError`
  (never silently reaching downstream math).
- `size = share_leg` (shares, human-scale) — **observational only**; never
  exposure / fill-volume / PnL / rankable.
- `aggressor_side` = the **taker** side = the negation of the maker's `side`
  (maker `side == 0` → BUY → aggressor SELL; `side == 1` → SELL → aggressor BUY).
- `token_id` = the non-`"0"` (outcome-token) assetId.
- Chain-event identity `(block_number, tx_hash, log_index)` for dedup + provenance.

---

## 5. Running a capture (operator)

```python
from veridex.maker.capture import capture_order_filled_artifact

# `client` is an operator-built HyperSync adapter that already holds HYPERSYNC_API
# and exposes .fetch_order_filled_logs(from_block=..., to_block=...).
artifact = capture_order_filled_artifact(
    from_block=<genesis_or_window_start>,
    to_block=<window_end>,
    out_path="scripts/txline_live/cp1/trade-artifact.json",
    client=operator_hypersync_client,
)
```

The entrypoint decodes every log, dedups by chain-event key, reconciles rows
against the pinned cp1 token set (`rows_matched_cp1` / `rows_unmatched` /
`rows_duplicate_dropped`), recomputes `artifact_hash`, stamps the pinned mapping
hash, and writes the validated JSON artifact. If any invariant fails
(reconciliation, duplicate event key, mapping pin, secret-bearing key), the
`TradeArtifact` constructor raises rather than writing a bad artifact.

---

## 6. Operator `prepare` / `seal` — the two-step capture-and-pin flow

A claimed sealed MM-R1.5 is a **two-step operator workflow** driven by the
composition-only CLI `scripts/maker/capture_and_pin.py`. It is deliberately SPLIT so
scoring can never self-pin its own config: an atomic capture-then-seal would set the
expected hash to `cfg.config_hash()` — a tautology that could never VOID. The two steps
are separated by a **committed predeclaration** (the pin-manifest).

### Step 1 — `prepare` (capture + predeclare)

```bash
export HYPERSYNC_API="<operator-token>"   # operator shell only; never committed
.venv/bin/python -m scripts.maker.capture_and_pin prepare \
    --from-block <window_start> --to-block <window_end> \
    --out scripts/txline_live/cp1/cp1-trades.json
```

`prepare` performs the live capture (fail-closed if `HYPERSYNC_API` is unset and no
operator log-source adapter is wired — see §2 / below), writes the validated
`TradeArtifact` to `--out`, then **writes and prints** a pin-manifest beside it at
`<out>.pin.json`:

```json
{
  "trade_artifact_hash": "<sha256 over the normalized rows>",
  "config_hash": "<MakerRunConfig.config_hash()>",
  "out_path": "scripts/txline_live/cp1/cp1-trades.json",
  "from_block": <window_start>,
  "to_block": <window_end>,
  "rows_matched_cp1": <int>
}
```

This manifest is the **predeclaration**. `prepare` **NEVER seals** — it writes nothing
to the sealed `RESULT_PATH`.

**Live-pull adapter (operator-only, non-CI):** `capture.py` performs no network import;
it consumes an injected `OrderFilledLogSource`. The CLI builds that source in
`_operator_log_source()` by lazily importing an operator-supplied
`scripts/maker/hypersync_source.py` exposing `build_hypersync_source(token=...)`. When
that adapter is absent (as in CI/tests) the capture fails closed with a clear message.
The operator token is only handed to the adapter constructor — it is never printed,
logged, or written into any artifact / manifest.

### Step 2 — review, COMMIT the predeclaration, then `seal`

1. **Review** the printed `trade_artifact_hash` and `config_hash`.
2. **Commit** the pin-manifest (`<out>.pin.json`) to the repo — this freezes the
   predeclaration *before* scoring.
3. Run `seal` with the **committed** `config_hash` as the expected hash:

```bash
.venv/bin/python -m scripts.maker.capture_and_pin seal \
    --artifact scripts/txline_live/cp1/cp1-trades.json \
    --expected-config-hash <config_hash-from-the-committed-manifest>
```

`--expected-config-hash` is **required**. The CLI passes it straight through to
`run_maker_arena(cfg, expected_config_hash=HASH, ...)` and **never recomputes it from the
live cfg**. `run_maker_arena`'s `verify_pinned(cfg, HASH)` VOIDs (`MakerVoidError`,
before any I/O) if the live config drifted from the committed predeclaration — so a
mismatched or stale hash fails loudly instead of self-pinning a drifted config. On a
match, the run seals an honest MM-R1.5 (`real_executable_edge_bps` stays the literal
`null`).

### Evidence availability (what to commit)

The normalized, provenance-hashed `TradeArtifact` **is the public on-chain evidence
input** (decoded `OrderFilled` trades between other venue participants — never Veridex
fills). Judges need those exact bytes to re-verify a sealed R1.5:

- **Commit the `TradeArtifact` if it is small enough**, or **publish it as a downloadable
  artifact with its `artifact_hash` pinned** (the `config_hash` binds that hash, so any
  drift moves the seal).
- **Commit the pin-manifest** — it is the predeclaration the seal is checked against.
- Do **NOT** commit the raw HyperSync dump (large; `raw_artifact_hash` covers it for
  audit).
- **NEVER** commit the `HYPERSYNC_API` token.

---

## 7. Test isolation guarantee

No test in `tests/test_maker_capture.py` or `tests/test_maker_capture_and_pin.py`
touches the network:

- `test_capture_entrypoint_fails_closed_without_token` exercises the fail-closed
  path (no token, no client) and asserts no artifact file is written.
- The injected-client test uses a network-free fake `OrderFilledLogSource` and
  asserts the written artifact contains no `HYPERSYNC` string.
- `test_capture_module_imports_only_stdlib_and_veridex` statically asserts the
  module imports only stdlib + `veridex.*`.
- The `prepare`/`seal` CLI tests monkeypatch the capture (or exercise its fail-closed
  guard), assert the anti-self-pin split VOIDs on a wrong predeclared hash, and assert
  the operator token never appears in stdout / stderr / artifact / pin-manifest.
