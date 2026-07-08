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

## 6. Test isolation guarantee

No test in `tests/test_maker_capture.py` touches the network:

- `test_capture_entrypoint_fails_closed_without_token` exercises the fail-closed
  path (no token, no client) and asserts no artifact file is written.
- The injected-client test uses a network-free fake `OrderFilledLogSource` and
  asserts the written artifact contains no `HYPERSYNC` string.
- `test_capture_module_imports_only_stdlib_and_veridex` statically asserts the
  module imports only stdlib + `veridex.*`.
