"""Unit tests for ShopifySync (no network calls)."""

from unittest.mock import MagicMock, patch

import pytest

from src.shopify_sync import ShopifySync, _parse_next_link, load_config


# ── _parse_next_link ─────────────────────────────────────────────────────────


def test_parse_next_link_returns_page_info():
    header = (
        '<https://example.myshopify.com/admin/api/2026-01/products.json'
        '?limit=250&page_info=abc123>; rel="next"'
    )
    assert _parse_next_link(header) == "abc123"


def test_parse_next_link_returns_none_when_absent():
    assert _parse_next_link("") is None
    assert _parse_next_link('<url>; rel="previous"') is None


# ── load_config ──────────────────────────────────────────────────────────────


def test_load_config_env_overrides_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"shopify": {"shop_url": "from-file.myshopify.com", "access_token": "file-token"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOPIFY_SHOP_URL", "from-env.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "env-token")

    cfg = load_config(cfg_file)
    assert cfg["shopify"]["shop_url"] == "from-env.myshopify.com"
    assert cfg["shopify"]["access_token"] == "env-token"


def test_load_config_missing_file_returns_empty(tmp_path):
    cfg = load_config(tmp_path / "does_not_exist.json")
    assert cfg["shopify"]["shop_url"] == ""


# ── ShopifySync.sync ─────────────────────────────────────────────────────────


@pytest.fixture
def syncer():
    s = ShopifySync.__new__(ShopifySync)
    s._base = "https://test.myshopify.com/admin/api/2026-01"
    s._session = MagicMock()
    s._location_id = 1
    s._sku_map = {
        "SKU-A": {
            "variant_id": 10,
            "inventory_item_id": 20,
            "product_id": 30,
        }
    }
    return s


def test_sync_updates_inventory_and_price(syncer):
    syncer._post = MagicMock(return_value={})
    syncer._put = MagicMock(return_value={})

    variants = [{"sku": "SKU-A", "qty": 5, "price": 199.0}]
    stats = syncer.sync(variants)

    assert stats["inventory_updated"] == 1
    assert stats["price_updated"] == 1
    assert stats["not_found"] == 0
    assert stats["errors"] == 0

    syncer._post.assert_called_once_with(
        "inventory_levels/set.json",
        {"location_id": 1, "inventory_item_id": 20, "available": 5},
    )
    syncer._put.assert_called_once_with(
        "variants/10.json",
        {"variant": {"inventory_policy": "continue", "price": "199.0"}},
    )


def test_sync_counts_not_found(syncer):
    syncer._post = MagicMock(return_value={})
    syncer._put = MagicMock(return_value={})

    stats = syncer.sync([{"sku": "MISSING", "qty": 1}])
    assert stats["not_found"] == 1
    syncer._post.assert_not_called()


def test_sync_custom_field_map(syncer):
    syncer._post = MagicMock(return_value={})
    syncer._put = MagicMock(return_value={})

    variants = [{"article_no": "SKU-A", "stock": 3}]
    stats = syncer.sync(variants, field_map={"sku": "article_no", "qty": "stock"})

    assert stats["inventory_updated"] == 1


def test_sync_handles_error_gracefully(syncer):
    syncer._post = MagicMock(side_effect=RuntimeError("API error"))
    syncer._put = MagicMock(return_value={})

    stats = syncer.sync([{"sku": "SKU-A", "qty": 2}])
    assert stats["errors"] == 1
    assert stats["inventory_updated"] == 0

