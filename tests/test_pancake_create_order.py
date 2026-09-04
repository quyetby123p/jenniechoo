"""Test cho PancakePosClient.create_order — phần tạo đơn mới."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from app.exceptions import PancakeApiError, ValidationError
from app.pancake_pos_client import PancakePosClient

from tests.test_pancake_pos_client import _dummy_settings


VALID_PAYLOAD: dict[str, Any] = {
    "items": [{"variation_id": "var-1", "quantity": 2}],
    "shipping_address": {"full_name": "Somchai", "phone_number": "0812345678"},
    "total_price": 2338,
}


def _client(tmp_path: Path, **overrides) -> PancakePosClient:
    return PancakePosClient(
        settings=_dummy_settings(tmp_path, **overrides),
        logger=logging.getLogger("test"),
    )


def test_goi_dung_endpoint_va_method(tmp_path: Path) -> None:
    client = _client(tmp_path)
    seen: dict[str, Any] = {}

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        seen.update({"method": method, "path": path, "data": data})
        return {"data": {"id": "PC-1"}}

    client._request = fake_request  # type: ignore[assignment]
    client.create_order(VALID_PAYLOAD)

    assert seen["method"] == "POST"
    assert seen["path"] == "/shops/123/orders"
    assert seen["data"] == VALID_PAYLOAD


def test_doc_danh_sach_nguon_don(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {"success": True, "data": [{"id": 42, "name": "Dropo"}]}  # type: ignore[assignment]

    assert client.list_order_sources() == [{"id": 42, "name": "Dropo"}]


def test_boc_don_khoi_envelope_data(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {"success": True, "data": {"id": "PC-9", "custom_id": "DROPO-1"}}  # type: ignore[assignment]
    created = client.create_order(VALID_PAYLOAD)
    assert created["id"] == "PC-9"
    assert created["custom_id"] == "DROPO-1"


def test_boc_don_khi_api_tra_thang_khong_boc(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {"id": "PC-7"}  # type: ignore[assignment]
    assert client.create_order(VALID_PAYLOAD)["id"] == "PC-7"


def test_thieu_shop_id_thi_tu_choi(tmp_path: Path) -> None:
    client = _client(tmp_path, pancake_shop_id=0)
    called = {"n": 0}

    def fake_request(*a, **k):  # noqa: ANN001, ANN002
        called["n"] += 1
        return {}

    client._request = fake_request  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="PANCAKE_SHOP_ID"):
        client.create_order(VALID_PAYLOAD)
    assert called["n"] == 0, "không được gọi API khi thiếu shop_id"


def test_payload_rong_thi_tu_choi(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {}  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="rỗng"):
        client.create_order({})


def test_don_khong_co_item_thi_tu_choi(tmp_path: Path) -> None:
    """Chặn sớm: đơn không item mà lọt lên Pancake là đơn rác trong hệ thống thật."""
    client = _client(tmp_path)
    called = {"n": 0}

    def fake_request(*a, **k):  # noqa: ANN001, ANN002
        called["n"] += 1
        return {}

    client._request = fake_request  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="ít nhất 1 item"):
        client.create_order({"items": [], "total_price": 100})
    assert called["n"] == 0


def test_api_khong_tra_don_thi_bao_loi(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {}  # type: ignore[assignment]
    with pytest.raises(PancakeApiError, match="không trả về đơn"):
        client.create_order(VALID_PAYLOAD)


def test_doi_duoc_endpoint_qua_create_cfg(tmp_path: Path) -> None:
    """Pancake đổi đường dẫn thì chỉnh config, không phải sửa code."""
    client = _client(tmp_path)
    seen: dict[str, Any] = {}

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        seen.update({"method": method, "path": path})
        return {"id": "PC-2"}

    client._request = fake_request  # type: ignore[assignment]
    client.create_order(VALID_PAYLOAD, create_cfg={"method": "PUT", "path": "/shops/{shop_id}/orders/new"})

    assert seen == {"method": "PUT", "path": "/shops/123/orders/new"}


def test_path_template_sai_placeholder_bao_loi_ro(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._request = lambda *a, **k: {"id": "x"}  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="placeholder"):
        client.create_order(VALID_PAYLOAD, create_cfg={"path": "/shops/{shop}/orders"})
