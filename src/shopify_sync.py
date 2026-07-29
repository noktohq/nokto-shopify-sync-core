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
import time
import urllib.parse as ul
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2026-01"


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

    def __init__(
        self,
        shop_url: str,
        access_token: str,
        location_id: int | None = None,
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

    # ── Private HTTP helpers ─────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        while True:
            r = self._session.get(f"{self._base}/{endpoint}", params=params)
            if r.status_code == 429:
                time.sleep(int(float(r.headers.get("Retry-After", 2))))
                continue
            r.raise_for_status()
            return r.json()

    def _put(self, endpoint: str, body: dict) -> Any:
        while True:
            r = self._session.put(f"{self._base}/{endpoint}", json=body)
            if r.status_code == 429:
                time.sleep(int(float(r.headers.get("Retry-After", 2))))
                continue
            r.raise_for_status()
            return r.json()

    def _post(self, endpoint: str, body: dict) -> Any:
        while True:
            r = self._session.post(f"{self._base}/{endpoint}", json=body)
            if r.status_code == 429:
                time.sleep(int(float(r.headers.get("Retry-After", 2))))
                continue
            r.raise_for_status()
            return r.json()

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
        """Returns SKU → variant metadata for every product in the store (cached)."""
        if self._sku_map is not None:
            return self._sku_map
        log.info("Building SKU map from Shopify…")
        sku_map: dict[str, dict] = {}
        params: dict = {"limit": 250, "fields": "id,variants"}

        while True:
            data = self._get("products.json", params=params)
            for product in data.get("products", []):
                for variant in product.get("variants", []):
                    sku = (variant.get("sku") or "").strip()
                    if sku:
                        sku_map[sku] = {
                            "variant_id": variant["id"],
                            "inventory_item_id": variant["inventory_item_id"],
                            "product_id": product["id"],
                        }

            next_page = _parse_next_link(
                self._session.get(
                    f"{self._base}/products.json", params=params
                ).headers.get("Link", "")
            )
            if not next_page:
                break
            params = {"limit": 250, "fields": "id,variants", "page_info": next_page}

        self._sku_map = sku_map
        log.info("Found %d SKUs in Shopify.", len(sku_map))
        return sku_map

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(
        self,
        variants: list[dict],
        field_map: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """
        Sync inventory quantity and price for each variant.

        Returns a stats dict:
            inventory_updated, price_updated, not_found, errors
        """
        fm = {**self.DEFAULT_FIELD_MAP, **(field_map or {})}
        location_id = self.get_location_id()
        sku_map = self.build_sku_map()

        stats: dict[str, int] = {
            "inventory_updated": 0,
            "price_updated": 0,
            "not_found": 0,
            "errors": 0,
        }

        total = len(variants)
        log.info("Syncing %d variants…", total)

        for i, variant in enumerate(variants, 1):
            sku = (variant.get(fm["sku"]) or "").strip()
            shopify = sku_map.get(sku)

            if not shopify:
                stats["not_found"] += 1
                continue

            if i % 50 == 0:
                log.info("  [%d/%d] processed", i, total)

            try:
                qty = int(variant.get(fm["qty"], 0) or 0)
                self._post(
                    "inventory_levels/set.json",
                    {
                        "location_id": location_id,
                        "inventory_item_id": shopify["inventory_item_id"],
                        "available": qty,
                    },
                )
                stats["inventory_updated"] += 1

                patch: dict = {
                    "inventory_policy": "continue" if qty > 0 else "deny"
                }
                price_raw = variant.get(fm["price"])
                if price_raw is not None:
                    patch["price"] = str(round(float(price_raw), 2))
                    stats["price_updated"] += 1

                self._put(
                    f"variants/{shopify['variant_id']}.json",
                    {"variant": patch},
                )
            except Exception as exc:
                log.warning("Error for SKU %s: %s", sku, exc)
                stats["errors"] += 1

        log.info(
            "Done — inventory: %d, price: %d, not found: %d, errors: %d",
            stats["inventory_updated"],
            stats["price_updated"],
            stats["not_found"],
            stats["errors"],
        )
        return stats


# ── Helpers ──────────────────────────────────────────────────────────────────


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
) -> None:
    """Convenience entry point."""
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
        return

    ShopifySync(shop_url, access_token, location_id).sync(
        variants, field_map=field_map
    )

