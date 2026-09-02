from __future__ import annotations

import logging
from typing import Any

from app.pancake_auto_confirm import (
    CONFIRMED_STATUS,
    PancakeAutoConfirmService,
    STATUS_UPDATE_CONFIG,
    WAITING_CONFIRMATION_STATUS,
)


class FakePancake:
    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self.orders = orders
        self.fetch_calls: list[tuple[int, int]] = []
        self.status_updates: list[dict[str, Any]] = []

    def fetch_orders_by_timestamp_range(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        self.fetch_calls.append((start_ts, end_ts))
        return list(self.orders)

    def update_order_status(self, order_id: str, status: int, *, update_cfg: dict[str, Any]) -> dict[str, Any]:
        self.status_updates.append(
            {"order_id": order_id, "status": status, "update_cfg": dict(update_cfg)}
        )
        return {"success": True}


def test_only_waiting_confirmation_orders_are_updated() -> None:
    pancake = FakePancake(
        [
            {"id": "waiting", "custom_id": "JCT100", "status": WAITING_CONFIRMATION_STATUS},
            {"id": "waiting_string", "custom_id": "JCT101", "status": "17"},
            {"id": "waiting_stock", "custom_id": "JCT102", "status": 11},
            {"id": "confirmed", "custom_id": "JCT103", "status": CONFIRMED_STATUS},
        ]
    )

    report = PancakeAutoConfirmService(pancake, logging.getLogger("test")).run_once(
        lookback_hours=168,
        max_batch=500,
    )

    assert report["ok"] is True
    assert report["fetched"] == 4
    assert report["candidates"] == 2
    assert report["updated"] == 2
    assert report["skipped"] == 0
    assert report["failed"] == 0
    assert [item["order_id"] for item in pancake.status_updates] == ["waiting", "waiting_string"]
    assert all(item["status"] == CONFIRMED_STATUS for item in pancake.status_updates)
    assert all(item["update_cfg"] == STATUS_UPDATE_CONFIG for item in pancake.status_updates)
    assert pancake.fetch_calls[0][1] - pancake.fetch_calls[0][0] == 168 * 3600


def test_fetch_error_does_not_attempt_any_update() -> None:
    class BrokenPancake(FakePancake):
        def fetch_orders_by_timestamp_range(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
            raise RuntimeError("temporary Pancake failure")

    pancake = BrokenPancake([])
    report = PancakeAutoConfirmService(pancake, logging.getLogger("test")).run_once()

    assert report["ok"] is False
    assert report["failed"] == 1
    assert report["updated"] == 0
    assert "temporary Pancake failure" in report["errors"][0]


def test_product_not_ready_is_skipped_without_failing_workflow() -> None:
    class NotReadyPancake(FakePancake):
        def update_order_status(self, order_id: str, status: int, *, update_cfg: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError('Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm"}')

    pancake = NotReadyPancake(
        [{"id": "waiting", "custom_id": "JCT102", "status": WAITING_CONFIRMATION_STATUS}]
    )
    report = PancakeAutoConfirmService(pancake, logging.getLogger("test")).run_once()

    assert report["ok"] is True
    assert report["updated"] == 0
    assert report["skipped"] == 1
    assert report["failed"] == 0
    assert len(report["skipped_reasons"]) == 1
