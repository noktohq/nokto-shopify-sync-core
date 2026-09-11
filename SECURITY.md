# Security policy

## Supported version

Security fixes are applied to the latest `main` branch only.

## Reporting

Report suspected vulnerabilities privately to `edin@nokto.no`. Do not open a
public issue for a security report. Do not include a real
`SHOPIFY_ACCESS_TOKEN`, client secret, or shop data in the report.

## Security model

- The Shopify Admin API access token is read from `config.json` or the
  `SHOPIFY_ACCESS_TOKEN` environment variable — never hardcoded, never logged.
  `config.json` and `.env` are gitignored.
- `sync_to_shopify` also supports OAuth client-credentials token exchange
  (`client_id` + `client_secret` in config) as an alternative to a static
  access token.
- The Admin API token needs write access to Products and Inventory (the
  client calls `variants/{id}.json` and `inventory_levels/set.json`). Scope
  the custom app's token to only what it needs.
- This is a synchronous REST client with no built-in secret redaction in
  logs. `sync()` logs SKU and error text on failure — do not log raw HTTP
  response bodies in your own wrapper without checking for tokens first.

## Known limitations

- `_get`/`_put`/`_post` retry indefinitely on HTTP 429 (rate limit), sleeping
  for the `Retry-After` header value each time. There is no maximum retry
  count and no backoff cap — a persistently rate-limited store can make
  `sync()` run for a long time rather than fail fast.
- These same HTTP calls have **no request timeout**. Only the OAuth
  client-credentials token exchange (`_get_token_via_client_credentials`)
  sets `timeout=10`. A hung connection to Shopify can block `sync()`
  indefinitely.
- Non-429 HTTP errors (4xx/5xx) are not retried — `raise_for_status()` raises
  immediately and the error is caught per-variant in `sync()`, counted in
  `stats["errors"]`, and logged; the rest of the batch continues.
- There is no dry-run mode: `sync()` writes inventory and price changes
  directly.
