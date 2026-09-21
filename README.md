# nokto-shopify-sync-core

Generic Shopify Admin REST client for syncing inventory quantity and price from external sources. Retries on HTTP 429 using Shopify's `Retry-After` header, follows `Link` pagination automatically, and supports OAuth client-credentials token acquisition.

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

sync_to_shopify(variants, config)
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
# {"inventory_updated": 42, "price_updated": 38, "not_found": 3, "errors": 0}
```

### Timeouts & retries

Every request times out after 30s by default — override with `ShopifySync(shop_url, access_token, timeout=60)`. Responses rate-limited with `429` are retried automatically (honouring `Retry-After`) up to `ShopifySync.MAX_RETRIES` (5) times, after which the error is raised instead of retrying forever.

## Security boundaries

The Admin API access token is read from `config.json` or environment
variables and needs write access to Products and Inventory. See
[SECURITY.md](SECURITY.md) for the full security model and how to report a
vulnerability.

## Known limitations

- Non-429 errors are not retried; they are caught per-variant, counted in
  `stats["errors"]`, and logged, and the rest of the batch continues.
- No dry-run mode — `sync()` writes changes directly to Shopify.

## Tests

```bash
pytest tests/ -v
```

## Evidence

- `tests/test_shopify_sync.py` — 8 tests, all passing. HTTP calls are
  mocked (`unittest.mock.MagicMock`); the suite makes no real network calls
  and needs no Shopify credentials.
- CI (`.github/workflows/ci.yml`) runs `pytest tests/ -v` on every push and
  pull request.

