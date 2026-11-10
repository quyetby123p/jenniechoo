"""Repair one Jennie Choo Pancake order item without changing other order data.

The command is dry-run by default.  ``--live`` is required before the full
order payload is PUT back to Pancake.  It locates an order by the public JC
order code, resolves the requested variation from the live Pancake catalog,
and verifies the persisted item plus unchanged order guards afterwards.
"""

from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
import re
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.dropo_pancake_bridge import BridgeConfig, DropoPancakeBridge
from app.exceptions import PancakeApiError, ValidationError
from app.pancake_pos_client import PancakePosClient
from app.settings import load_settings
from scripts.run_dropo_pancake_bridge import _apply_config_defaults


def build_logger() -> logging.Logger:
    logger = logging.getLogger("repair_jennie_order_variation")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def _contains_exact(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_exact(child, expected) for child in value.values())
    if isinstance(value, list):
        return any(_contains_exact(child, expected) for child in value)
    return isinstance(value, str) and value.strip() == expected


def _order_id(order: dict[str, Any]) -> str:
    for key in ("id", "order_id", "orderId", "order_code"):
        value = order.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _order_day(custom_id: str) -> date:
    match = re.search(r"JC(\d{6})-", custom_id.upper())
    if not match:
        raise ValidationError("custom_id phải có dạng JCYYMMDD-HHMMSS.")
    return datetime.strptime(match.group(1), "%y%m%d").date()


def _find_order(client: PancakePosClient, custom_id: str) -> dict[str, Any]:
    day = _order_day(custom_id)
    orders = client.fetch_all_orders_for_range(
        day,
        day + timedelta(days=1),
        "Asia/Ho_Chi_Minh",
    )
    matches = [order for order in orders if _contains_exact(order, custom_id)]
    if len(matches) != 1:
        raise ValidationError(
            f"Không xác định duy nhất đơn {custom_id}: tìm thấy {len(matches)} kết quả."
        )
    if not _order_id(matches[0]):
        raise ValidationError(f"Pancake không trả order_id cho đơn {custom_id}.")
    return matches[0]


def _item_code(bridge: DropoPancakeBridge, item: dict[str, Any]) -> str:
    info = item.get("variation_info")
    info = info if isinstance(info, dict) else {}
    for value in (
        info.get("product_code"),
        info.get("custom_id"),
        info.get("sku"),
        item.get("product_code"),
        item.get("custom_id"),
        item.get("sku"),
    ):
        raw = str(value or "").strip()
        code = bridge._normalize_jennie_code(raw)
        if code.startswith("JC-") and re.search(r"\d", code):
            return code
        # Pancake order details can return a variation SKU with a human
        # description appended, e.g. ``JC-A-250-BE: AO REN MAU BE-S``.
        # Extract the product code before normalizing the full variation SKU.
        for match in re.finditer(r"JC(?:-[A-Z]+-\d+|[A-Z]+-?\d+)", raw, re.IGNORECASE):
            embedded = bridge._normalize_jennie_code(match.group(0))
            if embedded.startswith("JC-") and re.search(r"\d", embedded):
                return embedded
    return ""


def _replace_item_variation(
    bridge: DropoPancakeBridge,
    order: dict[str, Any],
    *,
    code: str,
    color: str,
    size: str,
    occurrence: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    items = order.get("items")
    if not isinstance(items, list):
        raise ValidationError("Đơn Pancake không có danh sách items.")
    normalized_code = bridge._normalize_jennie_code(code)
    candidates = [
        (index, item)
        for index, item in enumerate(items)
        if isinstance(item, dict) and _item_code(bridge, item) == normalized_code
    ]
    if occurrence < 1 or occurrence > len(candidates):
        raise ValidationError(
            f"Đơn không có lần xuất hiện thứ {occurrence} của {normalized_code}; "
            f"tìm thấy {len(candidates)} item."
        )
    index, old_item = candidates[occurrence - 1]
    chosen = bridge._choose_jennie_variation(
        bridge._pancake_catalog.get(normalized_code, []),
        color=color,
        size=size,
    )
    if not chosen:
        raise ValidationError(
            f"Không tìm thấy biến thể {normalized_code} màu={color} size={size} trong catalog Pancake."
        )
    sku = str(chosen.get("variation_sku") or "").strip()
    variation_id = str(chosen.get("variation_id") or "").strip()
    if not sku or not variation_id:
        raise ValidationError("Catalog Pancake thiếu variation_sku hoặc variation_id.")

    new_order = copy.deepcopy(order)
    new_item = copy.deepcopy(old_item)
    old_info = old_item.get("variation_info")
    new_info = copy.deepcopy(old_info) if isinstance(old_info, dict) else {}
    new_info["id"] = variation_id
    new_info["variation_id"] = variation_id
    new_info["sku"] = sku
    new_info["custom_id"] = sku
    new_info["product_code"] = normalized_code
    if chosen.get("retail_price") not in (None, ""):
        new_info["retail_price"] = chosen["retail_price"]
    new_item["variation_info"] = new_info
    if "variation_id" in new_item:
        new_item["variation_id"] = variation_id
    if "sku" in new_item:
        new_item["sku"] = sku
    new_order["items"][index] = new_item
    return new_order, {
        "index": index,
        "old_sku": str((old_info or {}).get("sku", "")) if isinstance(old_info, dict) else "",
        "new_sku": sku,
        "variation_id": variation_id,
    }


def _guard(order: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(order)
    snapshot.pop("items", None)
    return snapshot


def repair(args: argparse.Namespace) -> int:
    logger = build_logger()
    _apply_config_defaults("")
    settings = load_settings(profile=None, require_app_credentials=False)
    config = BridgeConfig.from_env("")
    client = PancakePosClient(settings, logger)
    if not client.is_configured():
        raise ValidationError("Thiếu PANCAKE_ACCESS_TOKEN hoặc PANCAKE_SHOP_ID.")
    bridge = DropoPancakeBridge(settings, logger, config=config, pancake_client=client)
    if not bridge._load_pancake_sku_map():
        raise ValidationError("Không đọc được catalog Pancake.")

    summary = _find_order(client, args.custom_id)
    order_id = _order_id(summary)
    current = client.get_order_detail(order_id)
    updated, change = _replace_item_variation(
        bridge,
        current,
        code=args.code,
        color=args.color,
        size=args.size,
        occurrence=args.occurrence,
    )
    if change["old_sku"] == change["new_sku"]:
        logger.info("Đơn %s đã đúng biến thể %s; không cần cập nhật.", args.custom_id, change["new_sku"])
        return 0
    if not args.live:
        logger.info(
            "DRY RUN %s: %s -> %s (màu=%s, size=%s)",
            args.custom_id,
            change["old_sku"],
            change["new_sku"],
            args.color,
            args.size,
        )
        return 0

    path = client._format_order_path("/shops/{shop_id}/orders/{order_id}", order_id)
    client._assert_only_allowed_field_changes(
        source=current,
        target=updated,
        allowed_paths=["items"],
    )
    client._request("PUT", path, data=updated)
    latest = client.get_order_detail(order_id)
    if _guard(current) != _guard(latest):
        raise PancakeApiError("Cập nhật đã làm thay đổi dữ liệu ngoài danh sách items.")
    persisted_items = latest.get("items")
    if not isinstance(persisted_items, list):
        raise PancakeApiError("Pancake không trả lại items sau khi cập nhật.")
    persisted = persisted_items[change["index"]]
    persisted_info = persisted.get("variation_info") if isinstance(persisted, dict) else {}
    persisted_sku = str((persisted_info or {}).get("sku", "")).strip() if isinstance(persisted_info, dict) else ""
    if persisted_sku != change["new_sku"]:
        raise PancakeApiError(
            f"Pancake chưa lưu đúng biến thể: expected={change['new_sku']}, actual={persisted_sku}."
        )
    logger.info("Đã sửa %s: %s -> %s", args.custom_id, change["old_sku"], persisted_sku)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sửa biến thể một đơn Jennie Choo trên Pancake.")
    parser.add_argument("--custom-id", required=True)
    parser.add_argument("--code", default="JC-A-250")
    parser.add_argument("--color", default="Đen")
    parser.add_argument("--size", default="S")
    parser.add_argument("--occurrence", type=int, default=1)
    parser.add_argument("--live", action="store_true")
    return repair(parser.parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, PancakeApiError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
