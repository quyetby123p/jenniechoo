"""Confirm Pancake orders after they leave the waiting-stock state.

This worker is intentionally independent from the Pancake -> Thai Duong sync
service. It may only call the Pancake order-status endpoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any

from app.logger import configure_logger
from app.pancake_pos_client import PancakePosClient
from app.settings import load_settings


WAITING_CONFIRMATION_STATUS = 17
CONFIRMED_STATUS = 1
STATUS_UPDATE_CONFIG = {
    # Chỉ đổi trạng thái; không PUT lại toàn bộ items vì Pancake sẽ kiểm tra
    # tồn kho lần nữa và có thể trả 422 dù đơn đã hợp lệ.
    "method": "PUT",
    "path": "/shops/{shop_id}/orders/{order_id}",
    "status_field": "status",
    "verify_after_update": True,
    "extra_payload": {},
}


class PancakeAutoConfirmService:
    def __init__(self, pancake: PancakePosClient, logger: logging.Logger) -> None:
        self.pancake = pancake
        self.logger = logger

    def run_once(self, *, lookback_hours: int = 168, max_batch: int = 500) -> dict[str, Any]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        safe_lookback_hours = max(1, int(lookback_hours))
        safe_max_batch = max(1, int(max_batch))
        start_ts = now_ts - safe_lookback_hours * 3600
        summary: dict[str, Any] = {
            "ok": True,
            "lookback_hours": safe_lookback_hours,
            "fetched": 0,
            "candidates": 0,
            "updated": 0,
            "skipped": 0,
            "skipped_reasons": [],
            "failed": 0,
            "errors": [],
        }

        try:
            orders = self.pancake.fetch_orders_by_timestamp_range(start_ts, now_ts)
        except Exception as exc:  # noqa: BLE001
            summary["ok"] = False
            summary["failed"] = 1
            summary["errors"] = [f"Lấy đơn Pancake thất bại: {exc}"]
            return summary

        if not isinstance(orders, list):
            orders = []
        summary["fetched"] = len(orders)

        for order in orders:
            if summary["candidates"] >= safe_max_batch:
                break
            if not isinstance(order, dict):
                continue
            if self._to_int(order.get("status"), fallback=-1) != WAITING_CONFIRMATION_STATUS:
                continue

            summary["candidates"] += 1
            order_id = str(order.get("id") or "").strip()
            order_code = str(
                order.get("custom_id") or order.get("display_id") or order.get("code") or order_id
            ).strip()
            if not order_id:
                summary["failed"] += 1
                summary["errors"].append(f"Đơn {order_code or 'không mã'} thiếu order_id Pancake.")
                continue

            try:
                result = self.pancake.update_order_status(
                    order_id,
                    CONFIRMED_STATUS,
                    update_cfg=STATUS_UPDATE_CONFIG,
                )
                if isinstance(result, dict) and result.get("skipped"):
                    summary["skipped"] += 1
                else:
                    summary["updated"] += 1
            except Exception as exc:  # noqa: BLE001
                if self._is_not_ready_error(exc):
                    summary["skipped"] += 1
                    summary["skipped_reasons"].append(f"Đơn {order_code}: {exc}")
                    continue
                summary["failed"] += 1
                summary["errors"].append(f"Đổi trạng thái đơn {order_code} thất bại: {exc}")

        summary["ok"] = summary["failed"] == 0
        return summary

    @staticmethod
    def _to_int(value: Any, *, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _is_not_ready_error(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        return "chưa có thông tin sản phẩm" in text or "chua co thong tin san pham" in text


def _build_service(profile: str | None) -> PancakeAutoConfirmService:
    project_root = Path(__file__).resolve().parents[1]
    # Main profile dùng bộ biến PANCAKE_* không tiền tố. Chỉ profile phụ như
    # ads2 mới dùng nhóm biến có tiền tố; truyền "main" vào settings sẽ khiến
    # loader đòi MAIN_TELEGRAM_BOT_TOKEN dù worker chỉ cần Pancake.
    normalized_profile = str(profile or "").strip().lower()
    settings_profile = None if normalized_profile in {"", "main", "default"} else profile
    settings = load_settings(project_root=project_root, profile=settings_profile)
    logger = configure_logger(
        settings.app_logs_dir,
        secrets=[settings.pancake_api_key, settings.pancake_access_token],
    )
    return PancakeAutoConfirmService(PancakePosClient(settings, logger), logger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Confirm Pancake waiting-confirmation orders only.")
    parser.add_argument("--profile", choices=["main", "ads2"], default="main")
    parser.add_argument("--lookback-hours", type=int, default=168)
    parser.add_argument("--max-batch", type=int, default=500)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = _build_service(args.profile).run_once(
            lookback_hours=args.lookback_hours,
            max_batch=args.max_batch,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "failed": 1, "errors": [str(exc)]}, ensure_ascii=True))
        return 1

    print(json.dumps(report, ensure_ascii=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
