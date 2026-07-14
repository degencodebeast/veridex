# R4-A CLOB-V2 Wire Contract (REQ-017)

**Status:** DOCS/RESEARCH PIN. No production code, no network to any venue, no credentials, no
signing. Load-bearing spike (E3-T0) that gates all of E3/E4. Every wire interface below is pinned
from official primary sources (verbatim), with the fake-adapter emulated shape that E3-T2..T5/E4
tests assert against. Unknowns are marked **FAIL-CLOSED** — never guessed.

**Doctrine (`~/.claude/skills/python-error-handling`, fail-closed for unknowns):** pin verbatim, cite
every source, mark unknowns FAIL-CLOSED, never guess a fund-touching wire shape. Any wire field not
present in a pinned schema below MUST be treated as an unknown key → **fail closed** (reject), NOT
silently accepted. E3-T6's order commitment is **schema-derived from the pinned exact SET** below, not
a hand-maintained list.

## 0. Sources (library IDs / URLs / versions)

Context7 (primary):
- `/polymarket/py-clob-client-v2` — "PY Polymarket CLOB Client V2" (High reputation). The R4-A target SDK.
- `/polymarket/py-clob-client` — V1 (cross-reference only).
- `/websites/privy_io` — Privy (High reputation).

Exa fallback (official primary sources only, fetched 2026-07-12):
- `https://docs.polymarket.com/v2-migration` — "Migrating to CLOB V2" (effective **April 28, 2025**; V1 SDKs/V1-signed orders no longer supported on prod).
- `https://docs.polymarket.com/api-reference/trade/post-a-new-order` — OpenAPI `SendOrder`/`Order`/`SendOrderResponse` schemas + `securitySchemes` (POLY_* headers).
- `https://docs.polymarket.com/trading/orders/overview` — order types, OpenOrder object, Trade object, trade statuses.
- `https://docs.polymarket.com/trading/orders/cancel` — DELETE `/order` cancel response.
- `https://docs.polymarket.com/trading/orders/create` — statuses, error messages, post-only rules.
- `https://docs.polymarket.com/trading/clients/l1` — order signing (V2 struct fields).
- `https://docs.polymarket.com/api-reference/authentication` — L1 `ClobAuth` typed-data, L2 HMAC headers, signature-type table.
- `https://docs.polymarket.com/trading/fees` — fee formula, fee precision (round5), `getClobMarketInfo`.
- `https://docs.polymarket.com/trading/orders/attribution` — `builderCode` → `builder` field.
- Privy: `https://docs.privy.io/wallets/using-wallets/ethereum/sign-typed-data`, `https://docs.privy.io/api-reference/wallets/ethereum/eth-signtypeddata-v4`, `https://docs.privy.io/api-reference/intents/rpc`.

Vendored V1 client cross-checked (in-repo):
`veridex/venues/_vendor/polymarket_clob/client.py` — lines 46-105 (endpoints), 265-268 (TIF), 456-499
(order reads), 480-486 (cancel-all), 696-740 (HMAC + ClobAuth), 800-924 (order_to_json / OrderData /
domain), plus `get_contract_config` 652-690.

> **CONTRADICTION CHECK RESULT: NO CONTRADICTION.** The official CLOB-V2 docs **confirm** the
> approved contract in the task brief (V2 signed struct drops `taker/expiration/nonce/feeRateBps`,
> adds `timestamp/metadata/builder`; domain `version="2"`; V2 verifyingContracts). The **vendored
> in-repo client is V1** and MUST NOT be used to sign R4-A orders — see §11 deltas.

---

## 1. Order EIP-712 (V2 signed struct) — CONFIRMED

Source: `docs.polymarket.com/v2-migration`, `.../trading/clients/l1`, `/polymarket/py-clob-client-v2`
(`SignatureTypeV2`, `exchange_v2`/`neg_risk_exchange_v2`).

### 1a. EIP-712 domain (BOTH exchanges pinned)

```
{
  name: "Polymarket CTF Exchange",
  version: "2",                 # V1 was "1" — BUMPED in V2 (exchange domain only)
  chainId: 137,                 # Polygon mainnet
  verifyingContract: <exchange>
}
```

verifyingContract, chain 137 (both pinned):
- **Standard exchange (V2):** `0xE111180000d2663C0091e4f400237545B87B996B`
- **Neg-risk exchange (V2):** `0xe2222d279d744050d28e00520010520000310F59`

(V1 addresses `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` / `0xC5d563A36AE78145C45a50134d48A1215220f80a`
are REJECTED-value fixtures for R4-A — signing against them = fail closed.)

The `ClobAuthDomain` used for L1 API auth stays `version: "1"` (see §7). Only the exchange domain bumps to "2".

### 1b. Signed `Order` type (Solidity struct, in signing order)

```
Order(
  uint256 salt,
  address maker,
  address signer,
  uint256 tokenId,
  uint256 makerAmount,
  uint256 takerAmount,
  uint8   side,            # 0=BUY, 1=SELL (uint8 in signing payload)
  uint8   signatureType,
  uint256 timestamp,       # NEW: order creation time in MILLISECONDS; replaces nonce for per-address uniqueness (NOT an expiry)
  bytes32 metadata,        # NEW: reserved
  bytes32 builder          # NEW: builder-code attribution (bytes32), zero if none
)
```

REMOVED vs V1 signed struct: `taker`, `expiration`, `nonce`, `feeRateBps`. **These four are NOT in the
signed hash in V2.** (`expiration` still travels in the POST wire body — see §2 — but is NOT signed.)

Order value example (from v2-migration):

```
{ salt:"12345", maker:"0x…", signer:"0x…", tokenId:"102936…",
  makerAmount:"1000000", takerAmount:"2000000", side:0, signatureType:0,
  timestamp:"1713398400000",
  metadata:"0x00…00" (bytes32),
  builder:"0x00…00" (bytes32) }
```

### 1c. signatureType — EOA in scope, POLY_1271 out of scope

Source: `/polymarket/py-clob-client-v2` `order_utils/model/signature_type_v2.py`, docs `authentication`.

```python
class SignatureTypeV2(IntEnum):
    EOA = 0              # <-- R4-A: maker == signer == funder == Privy wallet (direct EOA)
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2
    POLY_1271 = 3        # ERC-1271 deposit-wallet model — OUT OF SCOPE (rejected-value fixture)
```

R4-A pins **`signatureType=0`** (direct EOA): `maker`, `signer`, and funder are all the single Privy
EOA address. `signatureType=3` (POLY_1271, deposit wallet) is retained ONLY as a rejected-value
fixture — an R4-A order carrying `3` must fail closed.

> **DOC INCONSISTENCY (non-blocking):** the OpenAPI `Order.signatureType` enum on
> `post-a-new-order` lists only `[0,1,2]`, while `authentication` + the SDK enum list `0,1,2,3`. R4-A
> only emits `0`, so this does not affect us; flagged for completeness.

**Fake emulated shape (E3-T6 cross-validation):** the fake `ExchangeOrderBuilderV2` MUST hash the
struct above with domain §1a. A digest fixture (`order_value` + expected `orderHash`) must be captured
from the **real** `py_clob_client_v2` `OrderBuilder.build_order(..., version=2)` and stored (see §10) —
NOT hand-derived. Until that real fixture is captured, the digest is **FAIL-CLOSED / operator-confirm**
(see §12).

---

## 2. Full `SendOrder` POST schema (exact SET) — CONFIRMED

Source: OpenAPI on `docs.polymarket.com/api-reference/trade/post-a-new-order`; wire example on `v2-migration`.
Route: `POST /order` (host `https://clob.polymarket.com`). SDK: `post_order(signed_order, order_type, post_only=…, defer_exec=…)`.

### 2a. Top-level SendOrder object — EXACT SET

| key | type | required | default | notes |
|---|---|---|---|---|
| `order` | Order (§2b) | yes | — | signed order object |
| `owner` | string | yes | — | **UUID of the API-key owner** (the L2 api_key). SECRET — never persisted/logged. |
| `orderType` | enum | no | `GTC` | one of `GTC` / `FOK` / `GTD` / `FAK` |
| `deferExec` | bool | no | `false` | defer execution |
| `postOnly` | bool | no | `false` | resting-only; GTC/GTD only (§6) |

### 2b. `order` object — EXACT SET (wire body, superset of the signed struct)

Required: `maker, signer, tokenId, makerAmount, takerAmount, side, expiration, timestamp, builder,
signature, salt, signatureType`. Optional property: `metadata`.

| key | type | in signed hash? | notes |
|---|---|---|---|
| `salt` | integer | yes | random uniqueness |
| `maker` | string(addr) | yes | funder (EOA in R4-A) |
| `signer` | string(addr) | yes | == maker for EOA |
| `tokenId` | string | yes | asset id |
| `makerAmount` | string | yes | fixed-math, 6 decimals |
| `takerAmount` | string | yes | fixed-math, 6 decimals |
| `side` | string `BUY`/`SELL` | yes (as uint8 0/1) | **wire = string; signed = uint8** |
| `expiration` | string(unix s) | **NO** | GTD/order-expiry only; NOT signed |
| `timestamp` | string(unix ms) | yes | creation ms, uniqueness |
| `metadata` | string(bytes32) | yes | reserved; optional in wire |
| `builder` | string(bytes32) | yes | attribution; `0x`+64 hex or empty |
| `signatureType` | integer | yes | R4-A = `0` |
| `signature` | string | n/a | the EIP-712 signature itself |

Absent vs V1 wire body: `taker`, `nonce`, `feeRateBps` (removed). Any key outside this SET → fail
closed (unknown key).

### 2c. `SendOrderResponse` — CONFIRMED

Required: `success` (bool), `orderID` (string = **order hash**), `status` (enum). Optional:
`makingAmount`, `takingAmount`, `transactionsHashes` (array; present when `matched`), `tradeIDs`
(array; present when `matched`), `errorMsg`.

`status` enum: `live` (resting), `matched` (immediate fill), `delayed` (async match delay),
`unmatched` (marketable but failed to delay; placement still successful).

Error placement failures carry `errorMsg` codes (e.g. `INVALID_ORDER_MIN_TICK_SIZE`,
`INVALID_ORDER_MIN_SIZE`, `INVALID_ORDER_NOT_ENOUGH_BALANCE`, `INVALID_ORDER_EXPIRATION`,
`INVALID_POST_ONLY_ORDER_TYPE`, `INVALID_POST_ONLY_ORDER`, `FOK_ORDER_NOT_FILLED_ERROR`).

**Fake emulated shape (E3-T2):** fake `post_order` returns
`{"success": true, "orderID": <computed order hash>, "status": "live", "makingAmount": "", "takingAmount": "", "transactionsHashes": [], "tradeIDs": [], "errorMsg": ""}`
for an accepted resting GTC; for an immediate fill: `status="matched"` with populated
`transactionsHashes` + `tradeIDs`. Rejections return `success=false` + non-empty `errorMsg`.

---

## 3. Trade / fill history — CONFIRMED

Source: `docs.polymarket.com/trading/orders/overview`, `.../cancel`; `/polymarket/py-clob-client-v2`
(`TradeParams`, `get_trades`, `get_trades_paginated`); endpoints.md (GET `/data/trades`).

### 3a. Methods / signatures

```python
# clob_types.py
@dataclass
class TradeParams:
    id: Optional[str] = None
    maker_address: Optional[str] = None
    market: Optional[str] = None          # condition id
    asset_id: Optional[str] = None        # token id
    before: Optional[int] = None
    after: Optional[int] = None

client.get_trades(params: TradeParams = None, only_first_page: bool = False) -> list[dict]
client.get_trades_paginated(params: TradeParams = None, page_size=…, max_pages=…) -> {trades|data, count, next_cursor}
```

Route: `GET /data/trades`. Auth: L2 optional (needed to filter by maker). Pagination: cursor
(`next_cursor`; `MA==` first page, `LTE=` terminal — same cursor scheme as V1).

### 3b. Trade object — EXACT SET

`id`, `taker_order_id` (hash), `market` (condition id), `asset_id` (token id), `side`, `size`,
`price`, `fee_rate_bps`, `status`, `match_time` (unix s), `last_update`, `outcome`, `owner` (API-key
id), `maker_address` (funder), `trader_side` (`TAKER`/`MAKER`), `transaction_hash`, `bucket_index`
(int; trade-reconciliation index), `maker_orders` (array of MakerOrder).

MakerOrder entry: `order_id` (hash), `owner`, `maker_address`, `matched_amount`, `price`,
`fee_rate_bps`, `asset_id`, `outcome`, `side`.

### 3c. Terminal-status mapping — CONFIRMED

| status | terminal? | meaning |
|---|---|---|
| `MATCHED` | No | matched, sent to executor for onchain submission |
| `MINED` | No | mined on chain, no finality yet |
| `CONFIRMED` | **Yes** | strong probabilistic finality — trade successful |
| `RETRYING` | No | tx failed (revert/reorg) — operator retrying |
| `FAILED` | **Yes** | failed permanently, not retried |

Terminal set = {`CONFIRMED`, `FAILED`}. Non-terminal = {`MATCHED`, `MINED`, `RETRYING`}. E4 must treat
only `CONFIRMED`/`FAILED` as settled; everything else is in-flight and must keep polling (fail-closed:
an unrecognized status string is in-flight, never treated as terminal).

### 3d. DURABLE PRE-SUBMIT JOIN KEY (E4 reconciliation) — CONFIRMED

The join key is the **EIP-712 signed-order hash** (`orderHash`), computable **locally before submit**
from the §1b V2 struct + §1a V2 domain. It equals:
- `SendOrderResponse.orderID` (documented as "order hash"),
- a trade's `taker_order_id` when we are taker,
- a `maker_orders[].order_id` when we are the resting maker.

**E4 MUST compute the hash with the V2 domain (`version="2"`, V2 verifyingContract) and the V2 struct.**
Computing it with V1 domain/struct yields a different hash and reconciliation will silently miss →
this is the single most load-bearing V1→V2 delta for E4 (see §11).

Secondary reconciliation aid (a SINGLE logical trade may split across onchain txs due to gas): join
related txs via (`bucket_index`, `match_time`). This is a within-trade aid, **not** the primary
order↔trade join key.

**Fake emulated shape (E4):** fake trade store keys trades by the same locally-computed `orderHash`;
`get_trades` returns Trade dicts (§3b) whose `taker_order_id` / `maker_orders[].order_id` equal that
hash, and advances `status` through the §3c ladder.

---

## 4. Single-order cancel — CONFIRMED (REAL route)

Source: `docs.polymarket.com/trading/orders/cancel`.

- **Route:** `DELETE /order` (host `https://clob.polymarket.com`). Body: `{"orderID": "<order hash>"}`.
  (NOT a nonexistent `/cancel`.)
- **SDK:** `client.cancel(order_id="0x…")` / `cancel_order(OrderPayload(orderID="0x…"))`.
- **Headers:** all 5 L2 `POLY_*` (§7).
- **Response (always this shape):** `{"canceled": ["0x…"], "not_canceled": {}}` where `canceled` =
  list of cancelled order ids and `not_canceled` = **map** of `orderId -> failure reason`.

Related (NOT single cancel): `DELETE /orders` body `["0x…","0x…"]` (batch), `DELETE /cancel-all`
(all), `DELETE /cancel-market-orders` body `{"market","asset_id"}` (by market). Same response shape.

**Fake emulated shape (E3-T4):** fake `cancel_order` removes the resting order and returns
`{"canceled": [order_id], "not_canceled": {}}`. Unknown/already-gone id returns
`{"canceled": [], "not_canceled": {order_id: "<reason>"}}` (fail-closed: never report a phantom cancel
as success).

---

## 5. Open / status reads — CONFIRMED (method names) / route UNCERTAIN

Source: `docs.polymarket.com/trading/orders/overview`; `/polymarket/py-clob-client-v2` (`OpenOrderParams`).

```python
@dataclass
class OpenOrderParams:
    id: Optional[str] = None
    market: Optional[str] = None
    asset_id: Optional[str] = None

client.get_order(order_id: str) -> dict                       # single order by id/hash
client.get_orders(params: OpenOrderParams = None) -> list[dict]   # (a.k.a. get_open_orders); paginated, L2
```

### OpenOrder object — EXACT SET

`id`, `status`, `market` (condition id), `asset_id` (token id), `side`, `original_size`,
`size_matched`, `price`, `outcome`, `order_type` (GTC/GTD/FOK/FAK), `maker_address` (funder), `owner`
(API-key id), `expiration` (`0` if none), `associate_trades` (string[] of trade ids), `created_at`.

**Route — FAIL-CLOSED / UNCERTAIN:** the exact V2 HTTP paths for `get_order` / `get_orders` are not
pinned by a V2 primary source in this spike. Vendored V1 uses `GET /data/order/{order_id}` and
`GET /data/orders` (client.py:81-88). These are **read-only, non-fund-touching**, so low blast radius,
but the V2 path is UNCERTAIN — implementers MUST confirm against the live V2 SDK route table before
relying on the literal path (§12).

**Fake emulated shape (E3-T3/T5):** fake `get_order` returns one OpenOrder dict (exact SET) reflecting
resting/partial state; `get_orders` returns a list, filterable by `market`/`asset_id`. `size_matched`
grows as fills arrive; on full fill/cancel the order leaves the open set and only appears via
`get_trades`.

---

## 6. Resting-maker wire (E3-T3 `RestingOrder`) — CONFIRMED

Source: `docs.polymarket.com/trading/orders/overview` (Order Types, Post-Only), `.../create`.

- **Post-only field name:** `postOnly` (bool) on the SendOrder top-level (§2a); NOT inside the signed
  order struct. SDK param `post_only=True` on `create_and_post_order` / `post_order`. Post-only is the
  ALO ("add-liquidity-only") semantic: if the order would cross the spread it is **rejected**
  (`INVALID_POST_ONLY_ORDER`), not executed. Post-only is valid ONLY with `GTC`/`GTD`; combined with
  `FOK`/`FAK` → rejected (`INVALID_POST_ONLY_ORDER_TYPE`).
- **GTC / GTD representation:** `orderType` string enum on SendOrder = `"GTC"` (rest until
  filled/cancelled) or `"GTD"` (rest until expiry). Both are limit/resting types.
- **GTD expiration field:** carried as `order.expiration` in the wire body (§2b) — **unix SECONDS**,
  UTC. NOT part of the signed hash. Semantics (load-bearing):
  - Orders expire **1 minute before** their stated expiration (security threshold).
  - Expiration must be **≥ 3 minutes** in the future.
  - For an effective lifetime of N minutes, set `expiration = now + 60s + N*60s`.
  - Past expiration → `INVALID_ORDER_EXPIRATION`.

**Fake emulated shape (E3-T3):** `RestingOrder` fake asserts: `orderType ∈ {GTC,GTD}`, `postOnly ∈
{true,false}`, and for GTD an `expiration` (unix s) satisfying the ≥3-min / −1-min rules. A post-only
order priced to cross returns `success=false, errorMsg="INVALID_POST_ONLY_ORDER"`; a post-only FOK/FAK
returns `INVALID_POST_ONLY_ORDER_TYPE`.

---

## 7. Auth — L1 `ClobAuth` + L2 HMAC — CONFIRMED (unchanged in V2)

Source: `docs.polymarket.com/api-reference/authentication`; vendored client.py:696-796 (identical shapes).

### 7a. L1 `ClobAuth` typed data (create/derive credentials)

Domain (stays `version:"1"` in V2):
```
{ name: "ClobAuthDomain", version: "1", chainId: 137 }
```
Types / value:
```
ClobAuth: [
  { name:"address",   type:"address" },
  { name:"timestamp", type:"string"  },
  { name:"nonce",     type:"uint256" },
  { name:"message",   type:"string"  },
]
value = { address:<signer>, timestamp:<server ts str>, nonce:<int, default 0>,
          message:"This message attests that I control the given wallet" }
```
L1 headers: `POLY_ADDRESS`, `POLY_SIGNATURE` (the EIP-712 sig), `POLY_TIMESTAMP`, `POLY_NONCE`.

L1 credential endpoints (vendored V1, unchanged in V2):
`POST /auth/api-key` (create), `GET /auth/derive-api-key` (derive); SDK
`create_api_key()` / `derive_api_key()` / `create_or_derive_api_key()` → `ApiCreds(api_key,
api_secret, api_passphrase)`.

### 7b. L2 HMAC (all trading endpoints)

L2 headers (all 5 required on trading endpoints): `POLY_ADDRESS`, `POLY_SIGNATURE` (HMAC),
`POLY_TIMESTAMP`, `POLY_API_KEY`, `POLY_PASSPHRASE`.

HMAC canonicalization (vendored client.py:696-707; docs say "HMAC-SHA256, reference impls in TS/Py
clients"):
```
base64_secret = base64.urlsafe_b64decode(api_secret)
message = str(timestamp) + str(method) + str(requestPath)
if body: message += str(body).replace("'", '"')   # single->double quote is LOAD-BEARING (matches TS/Go)
sig = base64.urlsafe_b64encode(HMAC_SHA256(base64_secret, message))
```
- Timestamp: unix **seconds** (vendored `_time('s')`).
- `requestPath` = endpoint path only; query params are passed separately and are **NOT** in the HMAC
  message in the vendored client. **FAIL-CLOSED note:** confirm the exact V2 SDK canonicalization
  (whether query string is included) before `l2_transport` ships (§12) — a mismatch here silently
  401s every trading call.
- `owner` (the `api_key` UUID) and `api_secret`/`api_passphrase` are SECRETS — never persisted or logged.

**Fake emulated shape (E3-T8 client + `l2_transport`):** fake transport asserts presence of the 5
`POLY_*` headers and that `POLY_SIGNATURE` was produced by the exact canonicalization above over
(timestamp, method, requestPath, normalized-body). L1 fake returns a deterministic `ApiCreds`.

---

## 8. Fee lookup + round5 — CONFIRMED

Source: `docs.polymarket.com/trading/fees`; `/polymarket/py-clob-client-v2` (`get_fee_rate_bps`,
`getClobMarketInfo`, endpoints.md).

- **Route:** `GET /clob-markets/{condition_id}`. SDK `getClobMarketInfo(conditionID)` /
  `get_clob_market_info(condition_id)`.
- **Response (EXACT SET):**
  ```json
  { "condition_id":"0x…",
    "t":[ {"t":"token_id_yes","o":"yes"}, {"t":"token_id_no","o":"no"} ],
    "mts": 0.01,          // minimum tick size
    "nr": false,          // neg-risk flag
    "fd": { "r": 0.05, "e": 1, "to": true } }   // fee descriptor
  ```
- **Fee parameter field:** `fd` = fee descriptor → `fd.r` = fee **rate**, `fd.e` = exponent, `fd.to` =
  taker-only flag. Market object also exposes `feesEnabled` (bool). Convenience:
  `client.get_fee_rate_bps(token_id) -> int` (bps, e.g. `50`).
- **Fee model (V2):** fees are **operator-set at match time**, NOT embedded in the signed order.
  Makers never charged; only takers. Formula: `fee = C × feeRate × p × (1-p)` (C = shares, p = price);
  USDC fee is symmetric around p=0.5.
- **round5 (venue precision) — CONFIRMED verbatim:** "Fees are rounded to **5 decimal places**. The
  smallest fee charged is **0.00001** USDC. Anything smaller **rounds to zero**." → rule: round to 5
  dp; magnitude `< 0.00001` → `0`; smallest nonzero = `0.00001`; **no upward floor** (small nonzero is
  NOT bumped up to the minimum — it drops to 0).

**Fake emulated shape (fee gate):** fake `get_clob_market_info` returns the exact `fd` SET; a
`round5(x)` helper implements `q = round(x, 5); return 0.0 if abs(q) < 0.00001 else q` and E-tests
assert `round5(0.000004) == 0.0`, `round5(0.00001) == 0.00001`, `round5(0.000006) == 0.00001` (nearest,
not floor), i.e. no upward flooring of sub-threshold values.

---

## 9. Named fixture + module paths (E3-T5 targets)

To be CREATED by E3 (do not exist yet; confirmed absent 2026-07-12):
- **CLOB-V2 gate module (exact path):** `veridex/dust_execution/clobv2_gate.py`
- **L2 transport:** `veridex/dust_execution/l2_transport.py`
- **Signature/payload fixtures (validated against current official schema in E3-T5):**
  `tests/fixtures/dust_execution/clobv2/` — proposed files:
  - `sendorder_gtc_eoa.json` — valid §2 SendOrder, `signatureType=0`, `postOnly=false`.
  - `sendorder_gtd_postonly.json` — GTD + postOnly=true, valid `expiration`.
  - `order_digest_v2.json` — captured `{domain, order_value, expected_orderHash}` from the real
    `py_clob_client_v2` builder (E3-T6 cross-validation). **Blocked until real digest captured (§12).**
  - `reject_sigtype3.json` — `signatureType=3` (POLY_1271) rejected-value fixture.
  - `reject_v1_domain.json` — order signed against V1 domain/verifyingContract (rejected).
  - `sendorder_response_matched.json`, `trade_confirmed.json`, `cancel_response.json` — §2c/§3b/§4 shapes.
- Existing dust tests live in `tests/test_dust_execution_*.py`; add `tests/test_dust_execution_clobv2_gate.py`.

---

## 10. Fake-adapter shapes — consolidated (so two implementers can't diverge)

The fake CLOB-V2 adapter used by E3-T2..T5/E4 MUST emulate exactly these shapes (all keys are the EXACT
SET from the pinned schemas; unknown key → fail closed):

| Interface | Fake input | Fake output |
|---|---|---|
| `post_order` (§2) | signed Order (§1b/§2b) + top-level (§2a) | `SendOrderResponse` (§2c): resting→`status:"live"`; fill→`status:"matched"`+`tradeIDs`/`transactionsHashes`; reject→`success:false`+`errorMsg` |
| `cancel_order` (§4) | `{"orderID": hash}` | `{"canceled":[…], "not_canceled":{…}}` |
| `get_order` (§5) | `order_id` | one OpenOrder dict (EXACT SET) or fail-closed if unknown |
| `get_orders` (§5) | `OpenOrderParams` | list[OpenOrder]; filter by market/asset_id |
| `get_trades` (§3) | `TradeParams` | list[Trade] (EXACT SET) keyed by local `orderHash`; status advances through §3c |
| `get_clob_market_info` (§8) | `condition_id` | `{condition_id,t,mts,nr,fd:{r,e,to}}` |
| L1 (§7a) | signer | deterministic `ApiCreds` |
| L2 transport (§7b) | method,path,body,creds | asserts 5 `POLY_*` headers + exact HMAC canonicalization |
| digest (§1) | `order_value` | `orderHash` (from REAL builder fixture — §12) |

---

## 11. EVERY delta vs the vendored V1 client (`veridex/venues/_vendor/polymarket_clob/client.py`)

The vendored client is **V1** and MUST NOT sign/submit R4-A orders. Deltas:

1. **Signed order struct** (client.py:900-912 `OrderData`): V1 signs `maker, taker, tokenId,
   makerAmount, takerAmount, side, feeRateBps, nonce, signer, expiration, signatureType`. **V2 removes
   `taker, expiration, nonce, feeRateBps`; adds `timestamp` (ms), `metadata` (bytes32), `builder`
   (bytes32).** (§1b)
2. **EIP-712 exchange domain version** (via `py_order_utils` in V1): `"1"` → **`"2"`** in V2. (§1a)
3. **verifyingContract** (client.py:652-690 `get_contract_config`): V1 standard
   `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` / neg-risk
   `0xC5d563A36AE78145C45a50134d48A1215220f80a` → **V2** `0xE111180000d2663C0091e4f400237545B87B996B`
   / `0xe2222d279d744050d28e00520010520000310F59`. (§1a)
4. **Order hash / join key**: because of 1-3, the V1-computed hash ≠ V2 hash. E4's pre-submit join key
   MUST use the V2 struct+domain. (§3d) — highest-risk delta.
5. **`side` encoding**: V1 `OrderData.side` uses `py_order_utils` BUY/SELL constants; V2 signs `side`
   as `uint8` (0/1) while the wire body still uses `"BUY"`/`"SELL"`. (Behaviorally same as V1 wire.)
6. **Fees**: V1 embeds `feeRateBps` in the signed order; **V2 sets fees operator-side at match time**
   (no fee field in the signed struct). (§8)
7. **Single-order cancel**: vendored V1 exposes **only** `cancel_all_orders` → `DELETE /cancel-all`
   (client.py:89-92, 480-486). It has **no** single `DELETE /order`. V2/R4-A REQUIRES `DELETE /order`
   with `{"orderID":…}`. (§4) — must be added.
8. **`post_order` wire wrapper** (client.py:800 `order_to_json`): V1 emits
   `{"order":…, "owner":…, "orderType":…}` with NO `postOnly`/`deferExec`. V2 SendOrder adds top-level
   **`postOnly`** and **`deferExec`**. (§2a)
9. **Builder attribution**: V1 used `POLY_BUILDER_*` HMAC headers + a builder-signing SDK; **V2 folds
   attribution into the single signed `builder` (bytes32) field** — the `POLY_BUILDER_*` headers are
   GONE. (§1b, §6)
10. **Collateral token**: V1 = USDC.e (`0x2791Bca1…`, client.py:659); **V2 = pUSD** (standard ERC-20
    backed by USDC). pUSD contract address is **FAIL-CLOSED / unknown** in this spike (§12). Not part
    of the order struct but relevant to allowances.
11. **TIF set** (client.py:265-268): V1 maps GTC/FOK/GTD and `FAK`(IOC). V2 canonical set is
    `GTC/GTD/FOK/FAK` (identical strings; no `IOC` alias in V2 wire). (§2a, §6)
12. **Onchain cancel**: V1 had onchain cancel via `nonce`; V2 replaces it with operator-controlled
    `pauseUser`/`unpauseUser` (out of R4-A scope; noted).
13. **L1/L2 auth**: **UNCHANGED** — `ClobAuthDomain` stays version `"1"`, same `POLY_*` headers, same
    HMAC canonicalization. (§7) (This is the one area with NO delta.)

---

## 12. Privy `eth_signTypedData_v4` — CONFIRMED

Source: Context7 `/websites/privy_io`
(`.../wallets/using-wallets/ethereum/sign-typed-data`, `.../api-reference/wallets/ethereum/eth-signtypeddata-v4`, `.../api-reference/intents/rpc`).

- **Route:** `POST https://api.privy.io/v1/wallets/{wallet_id}/rpc`.
- **Request body:**
  ```json
  { "method": "eth_signTypedData_v4",
    "params": { "typed_data": {
        "domain": {…}, "types": {…}, "primary_type": "<T>", "message": {…} } } }
  ```
  Optional top-level: `caip2`, `signature_options`, `address`, `chain_type:"ethereum"`, `wallet_id`.
  For the CLOB order, `typed_data` carries the §1a domain + §1b `Order` types + the order `message`
  (with `EIP712Domain` type entry included).
- **Response:**
  ```json
  { "method":"eth_signTypedData_v4",
    "data": { "signature":"0x…", "encoding":"hex" } }
  ```
  (JS SDK `signTypedData(walletId, typedData)` / provider `request({method,params:[address,typedData]})`
  return `{ signature }`.)
- **owner_id / quorum / policy resource-ownership:** Privy wallets are owned by an `owner`/`owner_id`
  and gated by a **key-quorum** authorization + **policy** engine; sensitive `/rpc` actions require an
  **authorization signature** over the request when the wallet/policy demands it (see
  `docs.privy.io` authorization-signatures + key-quorums). **FAIL-CLOSED:** the exact `owner_id` /
  quorum-threshold / policy-resource-ownership payload fields for THIS deployment's Privy wallet are
  deployment-specific and NOT pinned here — operator must confirm the authorization-signature
  requirement + owner/quorum config before E3-T8 signs a real order. Signing shape above is confirmed;
  the auth-signature wrapper is operator-confirm.

---

## 13. UNCERTAIN / FAIL-CLOSED — needs operator confirmation before E3-T2..T8 build

1. **§1 order digest fixture** — the real `py_clob_client_v2` `OrderBuilder.build_order(version=2)`
   digest (`order_value` → `orderHash`) was NOT executed in this docs-only spike. E3-T6's
   cross-validation fixture must be captured from the real builder; until then the digest is
   FAIL-CLOSED. **Highest priority.**
2. **§5 read routes** — exact V2 HTTP paths for `get_order` / `get_orders` (V1 used
   `/data/order/{id}`, `/data/orders`); confirm V2 path table. (Read-only, low blast radius.)
3. **§7b HMAC canonicalization** — confirm whether the V2 SDK includes the query string in
   `requestPath` and the exact body normalization; a mismatch 401s every trading call.
4. **§8 `fd` sub-fields** — `fd.to` (taker-only) confirmed on py sample; confirm `fd.e` exponent's use
   in the actual fee math for edge markets (0.0025 tick / world-cup markets).
5. **§11.10 pUSD collateral address** — V2 collateral token (pUSD) contract address not pinned by a
   reliable source (Context7 `configuration.md` gave `0xC011a7E1…` labelled "USDC", which is NOT
   Polygon USDC — treated as unreliable and rejected). Needed for allowances, not for the order struct.
6. **§2b `signatureType` enum** — OpenAPI lists `[0,1,2]` but SDK/auth list `0-3`; R4-A only emits `0`
   so non-blocking, flagged.

Everything else in §1-§8 is CONFIRMED with a primary-source citation and a pinned fake shape.
