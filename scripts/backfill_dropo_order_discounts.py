"""Sửa giá các đơn Dropo đã tạo nhưng Pancake chưa áp dụng giảm giá.

Mặc định chỉ audit. Thêm ``--live`` để cập nhật các đơn có tổng Pancake cao
hơn tổng khách trả trong Sheet. Script dùng full payload + guard, giữ nguyên
items, nguồn đơn và trạng thái; có thể chạy lại an toàn.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(REPO_ROOT.parent / "fb-ads-automation" / ".env")
except ImportError:  # pragma: no cover
    pass

from app.dropo_pancake_bridge import BridgeConfig, DropoPancakeBridge  # noqa: E402
from app.pancake_pos_client import PancakePosClient  # noqa: E402
from app.settings import load_settings  # noqa: E402


def _apply_config_defaults() -> None:
    config_path = REPO_ROOT / "config" / "dropo_pancake_bridge_jennie.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(config, dict):
        for key, value in config.items():
            name = str(key).strip().upper()
            if name and "TOKEN" not in name and "SECRET" not in name and "API_KEY" not in name:
                os.environ.setdefault(name, str(value))


def _sheet_orders(bridge: DropoPancakeBridge) -> list[tuple[str, int]]:
    values = bridge._fetch_sheet_values()
    if not values:
        return []
    header = [str(value).strip() for value in values[0]]
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for row in values[1:]:
        order_id = str(bridge._cell(row, header, "Pancake Order ID") or "").strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        total = bridge._to_minor(
            bridge._cell(row, header, "Tổng đơn", "Order value", "Giá trị đơn", "tong_don", "value"),
            100,
        )
        if total > 0:
            result.append((order_id, total))
    return result


def _update_pricing(
    pancake: PancakePosClient,
    bridge: DropoPancakeBridge,
    order_id: str,
    expected_total: int,
) -> tuple[int, int]:
    current = pancake.get_order_detail(order_id)
    actual_total = bridge._to_int(current.get("total_price"))
    if actual_total <= expected_total:
        return actual_total, 0
    discount = actual_total - expected_total
    payload = copy.deepcopy(current)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError("đơn không có items để phân bổ giảm giá")
    bridge._apply_item_discounts(items, discount)
    payload["total_price"] = expected_total
    payload["total_discount"] = discount
    path = f"/shops/{pancake.settings.pancake_shop_id}/orders/{order_id}"
    before_signature = pancake._capture_paths(current, ["__items_signature__", "status"])
    pancake._request("PUT", path, data=payload)
    latest = pancake.get_order_detail(order_id)
    after_signature = pancake._capture_paths(latest, ["__items_signature__", "status"])
    if before_signature != after_signature:
        raise RuntimeError("cập nhật giá làm thay đổi item hoặc trạng thái ngoài phạm vi")
    persisted_total = bridge._to_int(latest.get("total_price"))
    if persisted_total != expected_total:
        raise RuntimeError(f"Pancake lưu tổng {persisted_total}, cần {expected_total}")
    return persisted_total, discount


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill giảm giá Dropo cho đơn Jennie trên Pancake.")
    parser.add_argument("--live", action="store_true", help="Ghi thay đổi thật lên Pancake.")
    parser.add_argument("--order-id", action="append", default=[], help="Chỉ xử lý Order ID này; có thể lặp.")
    args = parser.parse_args()

    _apply_config_defaults()
    settings = load_settings(require_app_credentials=False)
    logger = logging.getLogger("backfill_dropo_order_discounts")
    logger.addHandler(logging.NullHandler())
    bridge = DropoPancakeBridge(settings, logger, config=BridgeConfig.from_env(""))
    pancake = PancakePosClient(settings, logger)
    orders = _sheet_orders(bridge)
    requested = {str(item).strip() for item in args.order_id if str(item).strip()}
    if requested:
        orders = [(order_id, total) for order_id, total in orders if order_id in requested]

    updated = skipped = failed = 0
    print(f"orders={len(orders)} live={args.live}")
    for order_id, expected_total in orders:
        try:
            current = pancake.get_order_detail(order_id)
            actual_total = bridge._to_int(current.get("total_price"))
            if actual_total <= expected_total:
                skipped += 1
                print(f"SKIP {order_id} pancake={actual_total} sheet={expected_total}")
                continue
            if not args.live:
                print(f"DRY  {order_id} pancake={actual_total} sheet={expected_total} discount={actual_total - expected_total}")
                continue
            persisted_total, discount = _update_pricing(pancake, bridge, order_id, expected_total)
            updated += 1
            print(f"OK   {order_id} total={persisted_total} discount={discount}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {order_id}: {exc}")

    print(f"updated={updated} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
