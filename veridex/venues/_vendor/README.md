# Vendored third-party code

Code vendored verbatim (or with only import-path rewrites) from external open-source projects,
kept out of the `pip` dependency graph as a package so it can be reviewed and pinned in-repo.

## `polymarket_clob/` — Polymarket CLOB client (REQ-2D-201)

- **Source:** `oracle-arb`'s BUNDLED `quantpylib` tree (a self-contained pure-Python fork with
  an INLINE `LOB` order-book class — no Cython `hft/lob.pyx` dependency), NOT the separate
  `quantpy-repo` package.
  - Upstream path (relative to the `tx-odds-hack` workspace root):
    `prediction-market-arbitrage/oracle-arb/quantpylib/`
- **Copy date:** 2026-07-02
- **License:** MIT (Polymarket, 2022) — reproduced verbatim in `client.py`.

### File mapping (upstream -> vendored)

| Upstream                                              | Vendored                                                          |
|--------------------------------------------------------|--------------------------------------------------------------------|
| `quantpylib/wrappers/polymarket.py` (incl. inline `LOB`) | `polymarket_clob/client.py`                                       |
| `quantpylib/standards/markets.py`                     | `polymarket_clob/markets.py`                                      |
| `quantpylib/throttler/httpx.py`                       | `polymarket_clob/throttler/httpx.py`                               |
| `quantpylib/throttler/aiohttp.py`                     | `polymarket_clob/throttler/aiohttp.py`                             |
| `quantpylib/throttler/exceptions.py`                  | `polymarket_clob/throttler/exceptions.py`                          |

### Local modifications

**Import-path rewrites ONLY** — no logic changes:

- upstream module `quantpylib.standards.markets`, aliased `as markets` -> local `veridex.venues._vendor.polymarket_clob.markets`, aliased `as markets`
- `from quantpylib.throttler.httpx import HTTPClient` -> `from veridex.venues._vendor.polymarket_clob.throttler.httpx import HTTPClient`
- `from quantpylib.throttler.aiohttp import request as _request` -> `from veridex.venues._vendor.polymarket_clob.throttler.aiohttp import request as _request`
- `from quantpylib.throttler.exceptions import HTTPException` -> `from veridex.venues._vendor.polymarket_clob.throttler.exceptions import HTTPException`

`markets.py` and `throttler/exceptions.py` have no internal `quantpylib.*` imports. Their logic
is verbatim from upstream; `throttler/exceptions.py` is byte-identical, and `markets.py` differs
only by trailing-whitespace normalization (a `diff -w` against upstream is clean).

### Import safety

Importing `polymarket_clob.client` (`LOB`, `Polymarket`) requires no credentials and makes no
network calls — the `Polymarket.__init__` constructs a signer and HTTP clients but does not
call out. `LOB` and the pricing helpers depend only on `numpy`; `Polymarket` (auth, order
signing) additionally needs `eth_account`, `eth_utils`, `py_order_utils`, and
`poly_eip712_structs`. These are declared under the `polymarket` extra in `pyproject.toml`, not
in the base dependency set.

### Depends on

Declared in `pyproject.toml` under `[project.optional-dependencies].polymarket`:
`py-order-utils`, `web3`, `eth-account`, `poly-eip712-structs`, `httpx`, `aiohttp`, `orjson`,
`numpy`.
