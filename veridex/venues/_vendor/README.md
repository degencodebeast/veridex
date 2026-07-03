# Vendored third-party code

Code vendored verbatim (or with only import-path rewrites) from external open-source projects,
kept out of the `pip` dependency graph as a package so it can be reviewed and pinned in-repo.

## `polymarket_clob/` — Polymarket CLOB client (REQ-2D-201)

- **Source:** a self-contained, pure-Python, MIT-licensed Polymarket CLOB client distribution.
- **Copy date:** 2026-07-02
- **License:** MIT (Polymarket, 2022) — reproduced verbatim in `client.py`.

### File mapping (upstream -> vendored)

| Upstream (client distribution)                  | Vendored                                                          |
|--------------------------------------------------|--------------------------------------------------------------------|
| `wrappers/polymarket.py` (incl. inline `LOB`)    | `polymarket_clob/client.py`                                       |
| `standards/markets.py`                           | `polymarket_clob/markets.py`                                      |
| `throttler/httpx.py`                             | `polymarket_clob/throttler/httpx.py`                               |
| `throttler/aiohttp.py`                           | `polymarket_clob/throttler/aiohttp.py`                             |
| `throttler/exceptions.py`                        | `polymarket_clob/throttler/exceptions.py`                          |

### Local modifications

**Import-path rewrites ONLY** — no logic changes. Every upstream-namespace import was rewritten to
the vendored namespace:

- the upstream `standards.markets` module, aliased `as markets` -> local
  `veridex.venues._vendor.polymarket_clob.markets`, aliased `as markets`
- the upstream `throttler.httpx` `HTTPClient` import -> `from veridex.venues._vendor.polymarket_clob.throttler.httpx import HTTPClient`
- the upstream `throttler.aiohttp` `request` import -> `from veridex.venues._vendor.polymarket_clob.throttler.aiohttp import request as _request`
- the upstream `throttler.exceptions` `HTTPException` import -> `from veridex.venues._vendor.polymarket_clob.throttler.exceptions import HTTPException`

`markets.py` and `throttler/exceptions.py` have no internal upstream-namespace imports. Their logic
is verbatim from upstream; `throttler/exceptions.py` is byte-identical, and `markets.py` differs
only by trailing-whitespace normalization (a `diff -w` against upstream is clean).

(`tests/test_vendor_imports.py` enforces the rewrite hygiene: no dangling upstream-namespace import
may survive anywhere under `veridex/`.)

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
