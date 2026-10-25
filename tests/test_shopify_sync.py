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


# ── Unknown quantity, conflicts, API errors, dry-run (regression) ────────────

import math  # noqa: E402

from src import shopify_sync as shopify_sync_module  # noqa: E402


def _with_writes(s, post_return=None, put_return=None):
    s._post = MagicMock(return_value={} if post_return is None else post_return)
    s._put = MagicMock(return_value={} if put_return is None else put_return)
    return s


def test_sync_missing_qty_is_unknown_and_not_written(syncer):
    """Regression: a row without qty used to be written as available=0."""
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A"}])

    syncer._post.assert_not_called()
    syncer._put.assert_not_called()
    assert stats["inventory_updated"] == 0
    assert stats["unknown_qty"] == 1
    assert stats["errors"] == 0
    assert stats.details == [
        {"sku": "SKU-A", "status": "unknown_qty", "reason": "qty missing or invalid", "value": "None"}
    ]


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "abc", "2.5", 2.5, float("nan"), float("inf"), True, False, [], {}],
)
def test_sync_invalid_qty_is_unknown_and_not_written(syncer, raw):
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A", "qty": raw, "price": 100}])

    syncer._post.assert_not_called()
    syncer._put.assert_not_called()
    assert stats["unknown_qty"] == 1
    assert stats["inventory_updated"] == 0
    assert stats["price_updated"] == 0


@pytest.mark.parametrize("raw,expected", [(0, 0), ("0", 0), (" 7 ", 7), (3.0, 3), ("+4", 4), (-2, -2)])
def test_sync_valid_qty_is_written(syncer, raw, expected):
    """A confirmed zero (or any valid integer) is still written."""
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A", "qty": raw}])

    syncer._post.assert_called_once_with(
        "inventory_levels/set.json",
        {"location_id": 1, "inventory_item_id": 20, "available": expected},
    )
    assert stats["inventory_updated"] == 1
    assert stats["unknown_qty"] == 0


def _page(products, next_token=None):
    headers = {}
    if next_token:
        headers["Link"] = (
            "<https://test.myshopify.com/admin/api/2026-01/products.json"
            f'?limit=250&page_info={next_token}>; rel="next"'
        )
    return _fake_response(json_data={"products": products}, headers=headers)


def test_build_sku_map_duplicate_sku_is_conflict_not_arbitrary_winner(real_syncer):
    """Regression: the last variant with a duplicated SKU used to win silently."""
    real_syncer._session.request.side_effect = [
        _page(
            [
                {"id": 1, "variants": [{"id": 11, "inventory_item_id": 111, "sku": "DUP"}]},
                {"id": 2, "variants": [{"id": 21, "inventory_item_id": 211, "sku": " DUP "}]},
                {"id": 3, "variants": [{"id": 31, "inventory_item_id": 311, "sku": "OK"}]},
            ]
        )
    ]

    sku_map = real_syncer.build_sku_map()

    assert set(sku_map) == {"OK"}
    conflicts = real_syncer.sku_conflicts
    assert set(conflicts) == {"DUP"}
    assert sorted(e["variant_id"] for e in conflicts["DUP"]) == [11, 21]


def test_build_sku_map_same_variant_on_two_pages_is_not_conflict(real_syncer):
    variant = {"id": 11, "inventory_item_id": 111, "sku": "A"}
    real_syncer._session.request.side_effect = [
        _page([{"id": 1, "variants": [variant]}], next_token="P2"),
        _page([{"id": 1, "variants": [variant]}]),
    ]

    sku_map = real_syncer.build_sku_map()

    assert set(sku_map) == {"A"}
    assert real_syncer.sku_conflicts == {}


def test_sync_skips_shopify_duplicate_sku_as_conflict(real_syncer):
    real_syncer._session.request.side_effect = [
        _page(
            [
                {"id": 1, "variants": [{"id": 11, "inventory_item_id": 111, "sku": "DUP"}]},
                {"id": 2, "variants": [{"id": 21, "inventory_item_id": 211, "sku": "DUP"}]},
            ]
        )
    ]
    _with_writes(real_syncer)

    stats = real_syncer.sync([{"sku": "DUP", "qty": 4}])

    real_syncer._post.assert_not_called()
    real_syncer._put.assert_not_called()
    assert stats["conflicts"] == 1
    assert stats["not_found"] == 0
    assert stats.details[0]["status"] == "conflict"
    assert stats.details[0]["variant_ids"] == [11, 21]


def test_sync_duplicate_input_sku_is_conflict(syncer):
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A", "qty": 1}, {"sku": "SKU-A", "qty": 5}])

    syncer._post.assert_not_called()
    assert stats["conflicts"] == 2
    assert stats["inventory_updated"] == 0
    assert stats.skus("conflict") == ["SKU-A", "SKU-A"]


def test_sync_api_errors_in_body_are_not_success(syncer):
    _with_writes(syncer, post_return={"errors": {"available": ["is invalid"]}})

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5, "price": 10}])

    assert stats["inventory_updated"] == 0
    assert stats["errors"] == 1
    syncer._put.assert_not_called()
    assert stats.details[0]["status"] == "error"
    assert stats.details[0]["inventory_written"] is False
    assert not stats.ok


def test_sync_inventory_response_mismatch_is_error(syncer):
    _with_writes(syncer, post_return={"inventory_level": {"available": 3}})

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5}])

    assert stats["inventory_updated"] == 0
    assert stats["errors"] == 1


def test_sync_variant_body_errors_are_counted(syncer):
    _with_writes(syncer, put_return={"errors": {"price": ["must be positive"]}})

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5, "price": 10}])

    assert stats["inventory_updated"] == 1
    assert stats["price_updated"] == 0
    assert stats["errors"] == 1
    assert stats.details[0]["inventory_written"] is True


@pytest.mark.parametrize("price", ["abc", float("nan"), -1, True])
def test_sync_invalid_price_writes_nothing(syncer, price):
    """Previously inventory was written before the price failed to parse."""
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5, "price": price}])

    syncer._post.assert_not_called()
    syncer._put.assert_not_called()
    assert stats["errors"] == 1


def test_sync_result_is_backwards_compatible_dict(syncer):
    _with_writes(syncer)

    stats = syncer.sync([{"sku": "SKU-A", "qty": 5}, {"sku": "NOPE", "qty": 1}])

    assert isinstance(stats, dict)
    for key in ("inventory_updated", "price_updated", "not_found", "errors"):
        assert key in stats
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in stats.values())
    assert stats.ok
    assert [d["status"] for d in stats.details] == ["updated", "not_found"]


def test_sync_dry_run_makes_no_writes(real_syncer):
    real_syncer._session.request.side_effect = [
        _page([{"id": 1, "variants": [{"id": 11, "inventory_item_id": 111, "sku": "A"}]}])
    ]

    stats = real_syncer.sync([{"sku": "A", "qty": 0, "price": 99}], dry_run=True)

    methods = [c.args[0] for c in real_syncer._session.request.call_args_list]
    assert methods == ["GET"]
    assert stats["inventory_updated"] == 0
    assert stats["would_update"] == 1
    assert stats.dry_run is True
    assert stats.details == [
        {
            "sku": "A",
            "status": "dry_run",
            "qty": 0,
            "variant": {"inventory_policy": "deny", "price": "99.0"},
        }
    ]


def test_dry_run_instance_blocks_writes_at_http_layer(real_syncer):
    real_syncer.dry_run = True

    with pytest.raises(shopify_sync_module.DryRunWriteError):
        real_syncer._post("inventory_levels/set.json", {"available": 1})

    real_syncer._session.request.assert_not_called()


def test_sync_to_shopify_passes_dry_run_and_returns_result(monkeypatch):
    seen = {}

    def fake_sync(self, variants, field_map=None, dry_run=None):
        seen["dry_run"] = dry_run
        return shopify_sync_module.SyncResult(dry_run=bool(dry_run))

    monkeypatch.setattr(shopify_sync_module.ShopifySync, "sync", fake_sync)
    cfg = {"shopify": {"shop_url": "t.myshopify.com", "access_token": "tok", "location_id": 1}}

    result = shopify_sync_module.sync_to_shopify([], cfg, dry_run=True)

    assert seen["dry_run"] is True
    assert isinstance(result, shopify_sync_module.SyncResult)


# ── Retries: reads bounded, mutations never blindly retried ─────────────────


def test_read_retries_transient_errors_bounded(real_syncer, monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: sleeps.append(s))
    real_syncer._session.request.side_effect = [
        requests.ConnectionError("reset"),
        _fake_response(status_code=503),
        _fake_response(json_data={"ok": True}),
    ]

    assert real_syncer._get("whatever.json") == {"ok": True}
    assert real_syncer._session.request.call_count == 3
    assert len(sleeps) == 2


def test_read_gives_up_on_persistent_5xx(real_syncer, monkeypatch):
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: None)
    real_syncer._session.request.side_effect = [
        _fake_response(status_code=503) for _ in range(ShopifySync.MAX_RETRIES + 1)
    ]

    with pytest.raises(requests.HTTPError):
        real_syncer._get("whatever.json")
    assert real_syncer._session.request.call_count == ShopifySync.MAX_RETRIES + 1


@pytest.mark.parametrize(
    "failure",
    [requests.ReadTimeout("read timed out"), requests.ConnectionError("reset")],
)
def test_mutation_with_unknown_outcome_is_not_retried(real_syncer, monkeypatch, failure):
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: None)
    real_syncer._sku_map = {"A": {"variant_id": 10, "inventory_item_id": 20, "product_id": 30}}
    real_syncer._session.request.side_effect = [failure, _fake_response(json_data={})]

    stats = real_syncer.sync([{"sku": "A", "qty": 2}])

    assert real_syncer._session.request.call_count == 1
    assert stats["errors"] == 1
    assert stats["inventory_updated"] == 0
    assert stats.details[0]["outcome_unknown"] is True


def test_mutation_5xx_is_not_retried(real_syncer, monkeypatch):
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: None)
    real_syncer._session.request.side_effect = [
        _fake_response(status_code=502),
        _fake_response(json_data={}),
    ]

    with pytest.raises(requests.HTTPError):
        real_syncer._post("inventory_levels/set.json", {"available": 1})
    assert real_syncer._session.request.call_count == 1


def test_mutation_429_is_retried_because_it_was_not_applied(real_syncer, monkeypatch):
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: None)
    real_syncer._session.request.side_effect = [
        _fake_response(status_code=429, headers={"Retry-After": "1.0"}),
        _fake_response(json_data={"inventory_level": {"available": 1}}),
    ]

    assert real_syncer._post("inventory_levels/set.json", {"available": 1}) == {
        "inventory_level": {"available": 1}
    }
    assert real_syncer._session.request.call_count == 2


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Wed, 21 Oct 2026 07:28:00 GMT", 2.0),  # unparseable → default wait
        ("1.5", 1.5),
        ("-3", 0.0),
        ("3600", 60.0),  # capped at MAX_RETRY_WAIT
        (None, 2.0),
    ],
)
def test_retry_after_is_parsed_safely_and_capped(real_syncer, monkeypatch, header, expected):
    sleeps = []
    monkeypatch.setattr("src.shopify_sync.time.sleep", lambda s: sleeps.append(s))
    headers = {"Retry-After": header} if header is not None else {}
    real_syncer._session.request.side_effect = [
        _fake_response(status_code=429, headers=headers),
        _fake_response(json_data={"ok": True}),
    ]

    assert real_syncer._get("whatever.json") == {"ok": True}
    assert len(sleeps) == 1
    assert math.isclose(sleeps[0], expected)


# ── Pagination ───────────────────────────────────────────────────────────────


def test_build_sku_map_paginates_many_products_and_variants(real_syncer):
    """More than one page and a product with more than 50 variants."""
    big = {
        "id": 1,
        "variants": [
            {"id": 1000 + n, "inventory_item_id": 2000 + n, "sku": f"BIG-{n}"} for n in range(60)
        ],
    }
    page1 = [big] + [
        {"id": 10 + n, "variants": [{"id": 3000 + n, "inventory_item_id": 4000 + n, "sku": f"P1-{n}"}]}
        for n in range(249)
    ]
    page2 = [
        {"id": 500 + n, "variants": [{"id": 5000 + n, "inventory_item_id": 6000 + n, "sku": f"P2-{n}"}]}
        for n in range(250)
    ]
    page3 = [{"id": 999, "variants": [{"id": 7000, "inventory_item_id": 8000, "sku": "LAST"}]}]
    real_syncer._session.request.side_effect = [
        _page(page1, next_token="T2"),
        _page(page2, next_token="T3"),
        _page(page3),
    ]

    sku_map = real_syncer.build_sku_map()

    assert len(sku_map) == 60 + 249 + 250 + 1
    assert sku_map["BIG-59"]["inventory_item_id"] == 2059
    assert sku_map["LAST"]["product_id"] == 999
    calls = real_syncer._session.request.call_args_list
    assert [c.kwargs["params"].get("page_info") for c in calls] == [None, "T2", "T3"]


def test_build_sku_map_repeated_cursor_stops_instead_of_looping(real_syncer):
    real_syncer._session.request.side_effect = [
        _page([], next_token="SAME"),
        _page([], next_token="SAME"),
        _page([], next_token="SAME"),
    ]

    with pytest.raises(RuntimeError):
        real_syncer.build_sku_map()
    assert real_syncer._session.request.call_count == 2


def test_parse_quantity_decimal_and_edge_values():
    from decimal import Decimal

    parse = shopify_sync_module.parse_quantity
    assert parse(Decimal("5")) == 5
    assert parse(Decimal("5.00")) == 5
    assert parse(Decimal("5.5")) is None
    assert parse(Decimal("NaN")) is None
    assert parse(Decimal("sNaN")) is None
    assert parse("0") == 0
    assert parse(b"3") is None
