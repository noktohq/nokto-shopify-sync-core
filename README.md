# nokto-shopify-sync-core

Generic Shopify Admin REST client for syncing inventory quantity and price from external sources. Retries on HTTP 429 using Shopify's `Retry-After` header (bounded), follows `Link` pagination automatically, supports OAuth client-credentials token acquisition and a dry-run mode.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Copy `config.example.json` → `config.json`, or use environment variables:

| Variable | Description |
|---|---|
| `SHOPIFY_SHOP_URL` | `your-store.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Admin API access token |
| `SHOPIFY_LOCATION_ID` | Location ID (optional — auto-detected) |

## Usage

```python
from src.shopify_sync import sync_to_shopify, load_config

config = load_config("config.json")

variants = [
    {"sku": "ABC-001", "qty": 12, "price": 399.0},
    {"sku": "ABC-002", "qty": 0,  "price": 399.0},
]

result = sync_to_shopify(variants, config)                 # writes
preview = sync_to_shopify(variants, config, dry_run=True)  # reads only
```

### Custom field names

If your data uses different keys, pass a `field_map`:

```python
from src.shopify_sync import ShopifySync

syncer = ShopifySync(shop_url, access_token)
stats = syncer.sync(
    variants,
    field_map={"sku": "article_no", "qty": "free_qty", "price": "list_price"},
)
print(stats)
# {"inventory_updated": 42, "price_updated": 38, "not_found": 3, "errors": 0,
#  "unknown_qty": 1, "conflicts": 2, "would_update": 0}
```

### Sync semantics

`sync()` returns a `SyncResult`: still a `dict` of int counters (the four
original keys are unchanged), plus `result.details` with one entry per input
row and `result.ok`.

| Situation | What happens | Reported as |
|---|---|---|
| `qty` missing, `None`, empty, not an integer (`"abc"`, `2.5`, `NaN`, `True`) | **Unknown, not 0.** Nothing is written for the row (no stock, no price) | `unknown_qty` |
| `qty` is a valid integer, including `0` | Stock and variant are written | `inventory_updated` |
| SKU used by more than one Shopify variant | Skipped; no variant wins arbitrarily. See `syncer.sku_conflicts` | `conflicts` |
| SKU appears more than once in the input | All those rows are skipped | `conflicts` |
| Invalid price (not a finite number ≥ 0) | Nothing is written for the row | `errors` |
| HTTP error, or a 2xx body with `errors` / unexpected `available` | Not counted as success | `errors` (`details[].outcome_unknown` is true when the write may still have been applied — reconcile before writing again) |
| `dry_run=True` | Only GET requests; writes are refused at the HTTP layer | `would_update` |

`result.ok` is false when any row ended in `errors`, `conflicts` or `unknown_qty`.
Use it to exit non-zero from a scheduled job.

### Timeouts & retries

Every request times out after 30s by default — override with `ShopifySync(shop_url, access_token, timeout=60)`.

- Reads (GET) are retried on 429, 500, 502, 503, 504, connection errors and timeouts.
- Writes (POST/PUT) are retried only on 429, where Shopify did not apply the request. A write that times out, loses its connection or gets a 5xx is **not** retried, because it may have been applied.
- The wait honours `Retry-After` (seconds; an unparseable value falls back to 2s), is capped at `ShopifySync.MAX_RETRY_WAIT` (60s), and at most `ShopifySync.MAX_RETRIES` (5) retries are made before the error is raised.
- Pagination stops with an error if Shopify repeats a `page_info` cursor.

## Security boundaries

The Admin API access token is read from `config.json` or environment
variables and needs write access to Products and Inventory. See
[SECURITY.md](SECURITY.md) for the full security model and how to report a
vulnerability.

## Known limitations

- Per-variant errors are caught, counted in `errors` and logged, and the rest
  of the batch continues.
- Without `location_id`, the first location returned by Shopify is used.
  Set `location_id` explicitly for stores with more than one location.
- No guard against an unexpectedly large drop in stock across the batch.

## Tests

```bash
pytest tests/ -v
```

## Evidence

- `tests/test_shopify_sync.py` — 62 tests, all passing (2026-10-17). HTTP
  calls are mocked (`unittest.mock.MagicMock`); the suite makes no real
  network calls and needs no Shopify credentials.
- CI (`.github/workflows/ci.yml`) runs `pytest tests/ -v` on every push and
  pull request.

