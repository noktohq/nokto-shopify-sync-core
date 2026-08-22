"""Unit tests for ShopifySync (no network calls)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.shopify_sync import (
    ShopifySync,
    _get_token_via_client_credentials,
    _parse_next_link,
    load_config,
)


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


def test_sync_partial_failure_does_not_overcount_price(syncer):
    """inventory POST succeeds but the price PUT fails: price_updated must not
    be counted for work that never actually completed, and the variant should
    only be reflected once, in `errors`."""
    syncer._post = MagicMock(return_value={})
    syncer._put = MagicMock(side_effect=RuntimeError("price API error"))

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5, "price": 199.0}])

    assert stats["inventory_updated"] == 1
    assert stats["price_updated"] == 0
    assert stats["errors"] == 1


# ── ShopifySync._request (retries, timeouts, pagination) ─────────────────────


def _fake_response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.headers = headers or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def real_syncer():
    s = ShopifySync("test.myshopify.com", "tok", location_id=1, timeout=5)
    s._session = MagicMock()
    return s


def test_request_passes_timeout_to_session(real_syncer):
    real_syncer._session.request.return_value = _fake_response(json_data={"ok": True})

    real_syncer._get("whatever.json")

    _, kwargs = real_syncer._session.request.call_args
    assert kwargs["timeout"] == 5


def test_request_retries_on_429_then_succeeds(real_syncer, monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: sleeps.append(s))

    rate_limited = _fake_response(status_code=429, headers={"Retry-After": "0"})
    ok = _fake_response(json_data={"ok": True})
    real_syncer._session.request.side_effect = [rate_limited, ok]

    result = real_syncer._get("whatever.json")

    assert result == {"ok": True}
    assert sleeps == [0]
    assert real_syncer._session.request.call_count == 2


def test_request_gives_up_after_max_retries(real_syncer, monkeypatch):
    """A store stuck under sustained rate-limiting must eventually raise,
    not retry forever."""
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: None)
    always_limited = [
        _fake_response(status_code=429, headers={"Retry-After": "0"})
        for _ in range(ShopifySync.MAX_RETRIES + 1)
    ]
    real_syncer._session.request.side_effect = always_limited

    with pytest.raises(requests.HTTPError):
        real_syncer._get("whatever.json")

    assert real_syncer._session.request.call_count == ShopifySync.MAX_RETRIES + 1


def test_build_sku_map_issues_one_request_per_page(real_syncer):
    """Regression test: build_sku_map must not re-fetch the same page a
    second time just to read the pagination Link header."""
    page1 = _fake_response(
        json_data={
            "products": [
                {"id": 1, "variants": [{"id": 10, "inventory_item_id": 20, "sku": "A"}]}
            ]
        },
        headers={
            "Link": (
                '<https://test.myshopify.com/admin/api/2026-01/products.json'
                '?limit=250&page_info=NEXT>; rel="next"'
            )
        },
    )
    page2 = _fake_response(
        json_data={
            "products": [
                {"id": 2, "variants": [{"id": 11, "inventory_item_id": 21, "sku": "B"}]}
            ]
        },
        headers={},
    )
    real_syncer._session.request.side_effect = [page1, page2]

    sku_map = real_syncer.build_sku_map()

    assert real_syncer._session.request.call_count == 2
    assert set(sku_map) == {"A", "B"}


# ── OAuth client-credentials ──────────────────────────────────────────────────


def test_get_token_via_client_credentials(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {"access_token": "shpat_xyz"}
    fake_post = MagicMock(return_value=fake_resp)
    monkeypatch.setattr("src.shopify_sync.requests.post", fake_post)

    token = _get_token_via_client_credentials("test.myshopify.com", "cid", "secret")

    assert token == "shpat_xyz"
    args, kwargs = fake_post.call_args
    assert args[0] == "https://test.myshopify.com/admin/oauth/access_token"
    assert kwargs["json"]["grant_type"] == "client_credentials"
