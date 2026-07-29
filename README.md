# nokto-shopify-sync-core

Generic Shopify Admin REST client for syncing inventory quantity and price from external sources. Handles rate limiting and pagination automatically, and supports OAuth client-credentials token acquisition.

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

## Tests

```bash
pytest tests/ -v
```

