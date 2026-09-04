"""Gán nguồn Dropo cho các đơn Jennie đã tạo trước đây.

Mặc định chỉ audit. Thêm ``--live`` để cập nhật các Pancake Order ID trong
Sheet Jennie Choo Đơn. Script có thể chạy lại an toàn: đơn đã đúng nguồn sẽ
được bỏ qua và khi sửa luôn dùng full payload + guard để không mất dữ liệu.
"""

from __future__ import annotations

import argparse
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
    if not isinstance(config, dict):
        return
    for key, value in config.items():
        name = str(key).strip().upper()
        if name and "TOKEN" not in name and "SECRET" not in name and "API_KEY" not in name:
            os.environ.setdefault(name, str(value))


def _source_id(sources: list[dict], source_name: str) -> int:
    expected = source_name.casefold()
    for source in sources:
        if str(source.get("name") or "").strip().casefold() == expected:
            try:
                value = int(source.get("id"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Nguồn {source_name} có ID không hợp lệ.") from exc
            if value > 0:
                return value
    raise RuntimeError(f"Không tìm thấy nguồn đơn {source_name!r} trong Pancake.")


def _sheet_order_ids(bridge: DropoPancakeBridge) -> list[str]:
    values = bridge._fetch_sheet_values()
    if not values:
        return []
    header = [str(value).strip() for value in values[0]]
    try:
        order_id_index = header.index("Pancake Order ID")
    except ValueError as exc:
        raise RuntimeError("Sheet chưa có cột Pancake Order ID.") from exc
    result: list[str] = []
    seen: set[str] = set()
    for row in values[1:]:
        if order_id_index >= len(row):
            continue
        order_id = str(row[order_id_index] or "").strip()
        if order_id and order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill nguồn Dropo cho đơn Jennie trên Pancake.")
    parser.add_argument("--live", action="store_true", help="Ghi thay đổi thật lên Pancake.")
    parser.add_argument("--order-id", action="append", default=[], help="Chỉ xử lý Order ID này; có thể lặp.")
    parser.add_argument("--source-name", default="Dropo", help="Tên nguồn cần gán.")
    args = parser.parse_args()

    _apply_config_defaults()
    settings = load_settings(require_app_credentials=False)
    logger = logging.getLogger("backfill_dropo_order_sources")
    logger.addHandler(logging.NullHandler())
    bridge = DropoPancakeBridge(settings, logger, config=BridgeConfig.from_env(""))
    pancake = PancakePosClient(settings, logger)

    source_name = str(args.source_name or "").strip() or "Dropo"
    source_id = _source_id(pancake.list_order_sources(), source_name)
    order_ids = list(dict.fromkeys(str(item).strip() for item in args.order_id if str(item).strip()))
    if not order_ids:
        order_ids = _sheet_order_ids(bridge)

    updated = skipped = failed = 0
    print(f"source={source_name} source_id={source_id} orders={len(order_ids)} live={args.live}")
    for order_id in order_ids:
        try:
            current = pancake.get_order_detail(order_id)
            current_source = str(current.get("ads_source") or "").strip()
            raw_order_sources = current.get("order_sources")
            if isinstance(raw_order_sources, list):
                current_source_ids = {str(item) for item in raw_order_sources}
            else:
                current_source_ids = {str(raw_order_sources)}
            if str(source_id) in current_source_ids and (
                not current_source or current_source.casefold() == source_name.casefold()
            ):
                skipped += 1
                print(f"SKIP {order_id} already={source_name}")
                continue
            if not args.live:
                print(f"DRY  {order_id} current={current_source or '-'}")
                continue
            pancake.update_order_source(order_id, source_id, source_name)
            updated += 1
            print(f"OK   {order_id} -> {source_name} ({source_id})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {order_id}: {exc}")

    print(f"updated={updated} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
