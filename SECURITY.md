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

- Retries are bounded: at most `MAX_RETRIES` (5) retries, each wait capped at
  `MAX_RETRY_WAIT` (60s). Reads retry on 429/5xx/connection errors; writes
  retry only on 429. Every request has a timeout (30s default).
- A write that fails with a timeout, connection error or 5xx is not retried
  and is reported with `outcome_unknown: true`; reconcile before writing again.
- Errors are caught per variant in `sync()`, counted in `errors`, and logged
  (SKU and error text, at most 300 characters); the rest of the batch continues.
- `dry_run=True` makes `sync()` read-only.
