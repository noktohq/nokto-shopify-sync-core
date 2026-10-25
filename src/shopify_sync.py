"""
Generic Shopify Admin REST client for inventory and price synchronisation.

Installation:
    pip install requests

Configuration (config.json or environment variables):
    SHOPIFY_SHOP_URL       — your-store.myshopify.com
    SHOPIFY_ACCESS_TOKEN   — Admin API access token
    SHOPIFY_LOCATION_ID    — Location ID (optional, auto-detected)
"""

import json
import logging
import math
import re
import time
import urllib.parse as ul
from collections import Counter
from decimal import Decimal
from numbers import Integral
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2026-01"


class DryRunWriteError(RuntimeError):
    """A write (POST/PUT/DELETE) was attempted while dry-run is active."""


class ShopifyResponseError(RuntimeError):
    """Shopify answered 2xx, but the body reports errors or an unexpected result."""


class SyncResult(dict):
    """
    Result of ``ShopifySync.sync``.

    Still a plain ``dict`` of int counters, so existing callers keep working.
    The legacy keys are ``inventory_updated``, ``price_updated``, ``not_found``
    and ``errors``; new keys are ``unknown_qty``, ``conflicts`` and
    ``would_update``. Per-row outcomes are in ``details`` (one entry per input
    row, ``status`` is one of ``updated``, ``not_found``, ``unknown_qty``,
    ``conflict``, ``error`` or ``dry_run``).
    """

    COUNTERS = (
        "inventory_updated",
        "price_updated",
        "not_found",
        "errors",
        "unknown_qty",
        "conflicts",
        "would_update",
    )

    def __init__(self, dry_run: bool = False) -> None:
        super().__init__({key: 0 for key in self.COUNTERS})
        self.dry_run = dry_run
        self.details: list[dict] = []

    def record(self, sku: str, status: str, **info: Any) -> None:
        self.details.append({"sku": sku, "status": status, **info})

    def skus(self, status: str) -> list[str]:
        """SKUs of every input row that ended with ``status``."""
        return [d["sku"] for d in self.details if d["status"] == status]

    @property
    def ok(self) -> bool:
        """True when no row failed, was in conflict or had an unknown quantity."""
        return self["errors"] == 0 and self["conflicts"] == 0 and self["unknown_qty"] == 0


class ShopifySync:
    """
    Synchronises product variants against the Shopify Admin REST API.

    Each variant dict must contain at least a SKU field.
    Use ``field_map`` to map your dict keys to the expected field names:

        field_map = {
            "sku":   "article_no",   # key that holds the SKU string
            "qty":   "free_qty",     # key that holds available quantity (int)
            "price": "list_price",   # key that holds price (float) — optional
        }
    """

    DEFAULT_FIELD_MAP: dict[str, str] = {
        "sku": "sku",
        "qty": "qty",
        "price": "price",
    }

    MAX_RETRIES = 5
    DEFAULT_RETRY_WAIT = 2.0  # seconds, when a 429 has no usable Retry-After
    MAX_RETRY_WAIT = 60.0  # upper bound for any single wait
    # Reads are retried on these; writes only on 429 (Shopify did not apply them).
    RETRYABLE_READ_STATUSES = frozenset({429, 500, 502, 503, 504})

    # Class-level defaults keep instances created without __init__ working.
    dry_run = False
    _timeout = 30.0
    _writes_blocked = False
    _sku_conflicts: dict[str, list[dict]] | None = None

    def __init__(
        self,
        shop_url: str,
        access_token: str,
        location_id: int | None = None,
        timeout: float = 30.0,
        dry_run: bool = False,
    ) -> None:
        self._base = f"https://{shop_url}/admin/api/{SHOPIFY_API_VERSION}"
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }
        )
        self._location_id = location_id
        self._sku_map: dict[str, dict] | None = None
        self._sku_conflicts = None
        self._timeout = timeout
        self.dry_run = dry_run

    # ── Private HTTP helpers ─────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> requests.Response:
        is_read = method.upper() == "GET"
        if not is_read and (self.dry_run or self._writes_blocked):
            raise DryRunWriteError(f"dry-run: refusing {method} {endpoint}")

        attempt = 0
        while True:
            try:
                r = self._session.request(
                    method,
                    f"{self._base}/{endpoint}",
                    params=params,
                    json=body,
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                # A write may have reached Shopify; never retry it blindly.
                if not is_read or attempt >= self.MAX_RETRIES:
                    raise
                attempt += 1
                wait = self._backoff(attempt)
                log.warning("%s %s failed (%s), retry %d in %.1fs", method, endpoint, exc, attempt, wait)
                time.sleep(wait)
                continue

            retryable = r.status_code == 429 or (
                is_read and r.status_code in self.RETRYABLE_READ_STATUSES
            )
            if retryable and attempt < self.MAX_RETRIES:
                attempt += 1
                wait = self._retry_wait(r, attempt)
                log.warning("%s %s → %s, retry %d in %.1fs", method, endpoint, r.status_code, attempt, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r

    def _backoff(self, attempt: int) -> float:
        return min(float(2 ** (attempt - 1)), self.MAX_RETRY_WAIT)

    def _retry_wait(self, r: requests.Response, attempt: int) -> float:
        try:
            wait = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError):
            wait = math.nan
        if not math.isfinite(wait):
            wait = self.DEFAULT_RETRY_WAIT if r.status_code == 429 else self._backoff(attempt)
        return min(max(wait, 0.0), self.MAX_RETRY_WAIT)

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        return self._request("GET", endpoint, params=params).json()

    def _put(self, endpoint: str, body: dict) -> Any:
        return self._request("PUT", endpoint, body=body).json()

    def _post(self, endpoint: str, body: dict) -> Any:
        return self._request("POST", endpoint, body=body).json()

    # ── Setup ────────────────────────────────────────────────────────────────

    def get_location_id(self) -> int:
        if self._location_id:
            return self._location_id
        locations = self._get("locations.json").get("locations", [])
        if not locations:
            raise RuntimeError("No locations found in Shopify store.")
        self._location_id = locations[0]["id"]
        log.info("Location: %s (id=%s)", locations[0]["name"], self._location_id)
        return self._location_id

    def build_sku_map(self) -> dict[str, dict]:
        """
        Returns SKU → variant metadata for every product in the store (cached).

        A SKU used by more than one Shopify variant is left out of the map and
        listed in ``sku_conflicts`` instead, so no variant wins arbitrarily.
        """
        if self._sku_map is not None:
            return self._sku_map
        log.info("Building SKU map from Shopify…")
        by_sku: dict[str, list[dict]] = {}
        params: dict = {"limit": 250, "fields": "id,variants"}
        seen_cursors: set[str] = set()

        while True:
            resp = self._request("GET", "products.json", params=params)
            data = resp.json()
            for product in data.get("products", []):
                for variant in product.get("variants", []):
                    sku = (variant.get("sku") or "").strip()
                    if not sku:
                        continue
                    entries = by_sku.setdefault(sku, [])
                    if any(e["variant_id"] == variant["id"] for e in entries):
                        continue  # same variant returned twice, not a conflict
                    entries.append(
                        {
                            "variant_id": variant["id"],
                            "inventory_item_id": variant["inventory_item_id"],
                            "product_id": product["id"],
                        }
                    )

            next_page = _parse_next_link(resp.headers.get("Link", ""))
            if not next_page:
                break
            if next_page in seen_cursors:
                raise RuntimeError(
                    "Shopify pagination repeated a page_info cursor; SKU map is incomplete."
                )
            seen_cursors.add(next_page)
            params = {"limit": 250, "fields": "id,variants", "page_info": next_page}

        sku_map = {sku: e[0] for sku, e in by_sku.items() if len(e) == 1}
        conflicts = {sku: e for sku, e in by_sku.items() if len(e) > 1}
        if conflicts:
            log.warning(
                "%d SKU(s) are used by more than one Shopify variant and will be skipped: %s",
                len(conflicts),
                ", ".join(sorted(conflicts)[:20]),
            )
        self._sku_map = sku_map
        self._sku_conflicts = conflicts
        log.info("Found %d SKUs in Shopify.", len(sku_map))
        return sku_map

    @property
    def sku_conflicts(self) -> dict[str, list[dict]]:
        """SKU → every Shopify variant that uses it (only SKUs used more than once)."""
        return dict(self._sku_conflicts or {})

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(
        self,
        variants: list[dict],
        field_map: dict[str, str] | None = None,
        dry_run: bool | None = None,
    ) -> SyncResult:
        """
        Sync inventory quantity and price for each variant.

        A missing or invalid quantity is unknown, not zero: nothing is written
        for that row. A SKU that appears on several Shopify variants, or more
        than once in ``variants``, is a conflict and is skipped. With
        ``dry_run=True`` only read requests are made.

        Returns a ``SyncResult`` (a dict of counters, see that class).
        """
        fm = {**self.DEFAULT_FIELD_MAP, **(field_map or {})}
        dry = self.dry_run if dry_run is None else bool(dry_run)
        location_id = self.get_location_id()
        sku_map = self.build_sku_map()
        shopify_conflicts = self._sku_conflicts or {}

        result = SyncResult(dry_run=dry)
        input_counts = Counter((v.get(fm["sku"]) or "").strip() for v in variants)

        total = len(variants)
        log.info("Syncing %d variants%s…", total, " (dry-run)" if dry else "")

        blocked_before = self._writes_blocked
        self._writes_blocked = blocked_before or dry
        try:
            for i, variant in enumerate(variants, 1):
                if i % 50 == 0:
                    log.info("  [%d/%d] processed", i, total)
                self._sync_one(variant, fm, location_id, sku_map, shopify_conflicts, input_counts, result)
        finally:
            self._writes_blocked = blocked_before

        log.info(
            "Done — inventory: %d, price: %d, not found: %d, unknown qty: %d, "
            "conflicts: %d, errors: %d%s",
            result["inventory_updated"],
            result["price_updated"],
            result["not_found"],
            result["unknown_qty"],
            result["conflicts"],
            result["errors"],
            f", would update: {result['would_update']}" if dry else "",
        )
        return result

    def _sync_one(
        self,
        variant: dict,
        fm: dict[str, str],
        location_id: int,
        sku_map: dict[str, dict],
        shopify_conflicts: dict[str, list[dict]],
        input_counts: Counter,
        result: SyncResult,
    ) -> None:
        sku = (variant.get(fm["sku"]) or "").strip()

        if sku in shopify_conflicts:
            result["conflicts"] += 1
            ids = [e["variant_id"] for e in shopify_conflicts[sku]]
            result.record(sku, "conflict", reason="SKU used by several Shopify variants", variant_ids=ids)
            log.warning("Conflict for SKU %s: used by Shopify variants %s — skipped", sku, ids)
            return

        shopify = sku_map.get(sku)
        if not shopify:
            result["not_found"] += 1
            result.record(sku, "not_found")
            return

        if input_counts[sku] > 1:
            result["conflicts"] += 1
            result.record(sku, "conflict", reason=f"SKU appears {input_counts[sku]} times in input")
            log.warning("Conflict for SKU %s: appears %d times in input — skipped", sku, input_counts[sku])
            return

        raw_qty = variant.get(fm["qty"])
        qty = parse_quantity(raw_qty)
        if qty is None:
            result["unknown_qty"] += 1
            result.record(sku, "unknown_qty", reason="qty missing or invalid", value=repr(raw_qty)[:60])
            log.warning("Unknown qty for SKU %s (%r) — nothing written", sku, raw_qty)
            return

        try:
            price = _format_price(variant.get(fm["price"]))
        except ValueError as exc:
            result["errors"] += 1
            result.record(sku, "error", reason=str(exc), inventory_written=False, outcome_unknown=False)
            log.warning("Invalid price for SKU %s: %s — nothing written", sku, exc)
            return

        patch: dict = {"inventory_policy": "continue" if qty > 0 else "deny"}
        if price is not None:
            patch["price"] = price

        if self._writes_blocked:
            result["would_update"] += 1
            result.record(sku, "dry_run", qty=qty, variant=patch)
            return

        inventory_written = False
        try:
            level = self._post(
                "inventory_levels/set.json",
                {
                    "location_id": location_id,
                    "inventory_item_id": shopify["inventory_item_id"],
                    "available": qty,
                },
            )
            _check_inventory_response(level, qty)
            inventory_written = True
            result["inventory_updated"] += 1

            _check_response(
                self._put(f"variants/{shopify['variant_id']}.json", {"variant": patch})
            )
            if price is not None:
                result["price_updated"] += 1
            result.record(sku, "updated", qty=qty, price=price)
        except Exception as exc:
            log.warning("Error for SKU %s: %s", sku, exc)
            result["errors"] += 1
            result.record(
                sku,
                "error",
                reason=str(exc)[:300],
                inventory_written=inventory_written,
                outcome_unknown=_write_outcome_unknown(exc),
            )


# ── Helpers ──────────────────────────────────────────────────────────────────

_INT_RE = re.compile(r"[+-]?\d+")


def parse_quantity(value: Any) -> int | None:
    """
    Returns the quantity as int, or None when it is unknown.

    None, empty strings, booleans, non-integral or non-finite numbers and
    anything else that is not clearly an integer are unknown — never 0.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if _INT_RE.fullmatch(text) else None
    return None


def _format_price(value: Any) -> str | None:
    """None/empty → no price update; otherwise a finite, non-negative number."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid price {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid price {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid price {value!r}")
    return str(round(number, 2))


def _check_response(data: Any) -> None:
    if isinstance(data, dict) and data.get("errors"):
        raise ShopifyResponseError(f"Shopify errors: {str(data['errors'])[:200]}")


def _check_inventory_response(data: Any, qty: int) -> None:
    _check_response(data)
    level = data.get("inventory_level") if isinstance(data, dict) else None
    if isinstance(level, dict) and "available" in level and level["available"] != qty:
        raise ShopifyResponseError(
            f"Shopify reports available={level['available']}, expected {qty}"
        )


def _write_outcome_unknown(exc: Exception) -> bool:
    """True when a failed write may still have been applied by Shopify."""
    if isinstance(exc, requests.ConnectTimeout):
        return False  # the request never reached Shopify
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return status is None or status >= 500
    return False


def _parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            url_part = part.split(";")[0].strip().strip("<>")
            qs = ul.parse_qs(ul.urlparse(url_part).query)
            return qs.get("page_info", [None])[0]
    return None


def load_config(path: str | Path = "config.json") -> dict:
    """Load config from JSON file, with environment variable overrides."""
    import os

    cfg: dict = {}
    p = Path(path)
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))

    shopify = cfg.get("shopify", {})
    return {
        **cfg,
        "shopify": {
            **shopify,
            "shop_url": os.getenv("SHOPIFY_SHOP_URL") or shopify.get("shop_url", ""),
            "access_token": (
                os.getenv("SHOPIFY_ACCESS_TOKEN") or shopify.get("access_token", "")
            ),
            "location_id": (
                int(os.getenv("SHOPIFY_LOCATION_ID", "0"))
                or shopify.get("location_id")
            ),
        },
    }


def _get_token_via_client_credentials(
    shop_url: str, client_id: str, client_secret: str
) -> str:
    r = requests.post(
        f"https://{shop_url}/admin/oauth/access_token",
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def sync_to_shopify(
    variants: list[dict],
    config: dict,
    field_map: dict[str, str] | None = None,
    dry_run: bool = False,
) -> SyncResult | None:
    """Convenience entry point. Returns the SyncResult, or None if config is missing."""
    shop_cfg = config.get("shopify", {})
    shop_url = shop_cfg.get("shop_url", "")
    client_id = shop_cfg.get("client_id", "")
    client_secret = shop_cfg.get("client_secret", "")
    location_id = shop_cfg.get("location_id") or None

    if client_id and client_secret:
        access_token = _get_token_via_client_credentials(
            shop_url, client_id, client_secret
        )
    else:
        access_token = shop_cfg.get("access_token", "")

    if not shop_url or not access_token:
        log.error("Missing shop_url or access_token in config.")
        return None

    return ShopifySync(shop_url, access_token, location_id).sync(
        variants, field_map=field_map, dry_run=dry_run
    )
