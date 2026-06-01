from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.exceptions import PancakeApiError, ValidationError
from app.settings import Settings


class PancakePosClient:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.base_url = settings.pancake_api_base_url

    def is_configured(self) -> bool:
        has_credential = bool(self.settings.pancake_access_token or self.settings.pancake_api_key)
        return has_credential and self.settings.pancake_shop_id > 0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.pancake_access_token and not self.settings.pancake_api_key:
            raise ValidationError(
                "Chưa có mã truy cập Pancake POS. Anh điền PANCAKE_ACCESS_TOKEN hoặc PANCAKE_API_KEY."
            )

        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        request_data = dict(data or {})
        if self.settings.pancake_access_token:
            request_params["access_token"] = self.settings.pancake_access_token
        else:
            request_params["api_key"] = self.settings.pancake_api_key

        attempts = max(1, self.settings.retry_max)
        for attempt in range(1, attempts + 1):
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    params=request_params,
                    json=request_data if request_data else None,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise PancakeApiError(f"Lỗi kết nối Pancake API: {exc}") from exc
                self._sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                retryable = response.status_code in {429, 500, 502, 503, 504}
                if retryable and attempt < attempts:
                    self._sleep_for_retry(attempt)
                    continue
                raise PancakeApiError(
                    f"Pancake API lỗi ({response.status_code}): {self._short_body(response.text)}"
                )

            payload = self._json_or_raise(response.text)
            if isinstance(payload, dict) and payload.get("success") is False:
                raise PancakeApiError(f"Pancake API báo lỗi nghiệp vụ: {self._short_body(response.text)}")
            return payload

        raise PancakeApiError("Pancake API lỗi không xác định.")

    def _sleep_for_retry(self, attempt: int) -> None:
        import time

        if attempt - 1 < len(self.settings.retry_backoff_seconds):
            delay = self.settings.retry_backoff_seconds[attempt - 1]
        else:
            delay = self.settings.retry_backoff_seconds[-1]
        time.sleep(max(0, delay))

    @staticmethod
    def _short_body(raw_text: str, max_len: int = 500) -> str:
        normalized = " ".join(str(raw_text).split())
        if len(normalized) <= max_len:
            return normalized
        return normalized[: max_len - 3] + "..."

    @staticmethod
    def _json_or_raise(raw_text: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PancakeApiError(f"Pancake API trả về JSON không hợp lệ: {raw_text[:300]}") from exc
        if not isinstance(payload, dict):
            raise PancakeApiError("Pancake API trả về dữ liệu không hợp lệ.")
        return payload

    def list_shops(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/shops")
        shops = payload.get("shops", [])
        if isinstance(shops, list):
            return [item for item in shops if isinstance(item, dict)]
        return []

    def fetch_all_orders_for_date(self, report_date: date, timezone_name: str) -> list[dict[str, Any]]:
        snapshot = self.fetch_daily_orders_snapshot(report_date, timezone_name)
        orders = snapshot.get("orders", [])
        if isinstance(orders, list):
            return [item for item in orders if isinstance(item, dict)]
        return []

    def fetch_daily_orders_snapshot(self, report_date: date, timezone_name: str) -> dict[str, Any]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")

        start_ts, end_ts = self._to_unix_day_range(report_date, timezone_name)
        orders, aggs = self._fetch_orders_by_timestamp_range_with_aggs(start_ts, end_ts)
        filtered_orders = self._filter_orders_by_local_date(orders, report_date, timezone_name)
        return {
            "orders": filtered_orders,
            "aggs": aggs if isinstance(aggs, dict) else {},
        }

    def fetch_all_orders_for_range(
        self,
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> list[dict[str, Any]]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        if end_date < start_date:
            raise ValidationError("Khoảng ngày Pancake không hợp lệ.")
        start_ts, _ = self._to_unix_day_range(start_date, timezone_name)
        _, end_ts = self._to_unix_day_range(end_date, timezone_name)
        return self._fetch_orders_by_timestamp_range(start_ts, end_ts)

    def fetch_orders_snapshot_for_range(
        self,
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> dict[str, Any]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        if end_date < start_date:
            raise ValidationError("Khoảng ngày Pancake không hợp lệ.")
        start_ts, _ = self._to_unix_day_range(start_date, timezone_name)
        _, end_ts = self._to_unix_day_range(end_date, timezone_name)
        orders, aggs = self._fetch_orders_by_timestamp_range_with_aggs(start_ts, end_ts)
        return {
            "orders": orders,
            "aggs": aggs if isinstance(aggs, dict) else {},
        }

    def fetch_orders_by_timestamp_range(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        if end_ts < start_ts:
            raise ValidationError("Khoảng thời gian Pancake không hợp lệ.")
        return self._fetch_orders_by_timestamp_range(start_ts, end_ts)

    def _fetch_orders_by_timestamp_range(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        orders, _ = self._fetch_orders_by_timestamp_range_with_aggs(start_ts, end_ts)
        return orders

    def _fetch_orders_by_timestamp_range_with_aggs(
        self,
        start_ts: int,
        end_ts: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        shop_id = self.settings.pancake_shop_id
        page_number = 1
        total_pages = 1
        orders: list[dict[str, Any]] = []
        aggs: dict[str, Any] = {}

        while page_number <= max(total_pages, 1):
            payload = self._request(
                "GET",
                f"/shops/{shop_id}/orders",
                params={
                    "page_size": self.settings.pancake_page_size,
                    "page_number": page_number,
                    "startDateTime": start_ts,
                    "endDateTime": end_ts,
                },
            )
            data = payload.get("data", [])
            if isinstance(data, list):
                orders.extend(item for item in data if isinstance(item, dict))
            payload_aggs = payload.get("aggs")
            if isinstance(payload_aggs, dict):
                aggs = payload_aggs

            total_pages = self._to_int(payload.get("total_pages"), fallback=page_number)
            if page_number >= total_pages or not data:
                break
            page_number += 1
        return orders, aggs

    def update_order_status(
        self,
        order_id: str,
        status: int,
        *,
        update_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        normalized_order_id = str(order_id).strip()
        if not normalized_order_id:
            raise ValidationError("Thiếu order_id để cập nhật trạng thái Pancake.")

        cfg = update_cfg if isinstance(update_cfg, dict) else {}
        method = str(cfg.get("method", "POST")).strip().upper() or "POST"
        path_template = str(cfg.get("path", "/shops/{shop_id}/orders/{order_id}")).strip()
        status_field = str(cfg.get("status_field", "status")).strip() or "status"
        extra_payload = cfg.get("extra_payload", {})
        payload: dict[str, Any] = {}
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        payload[status_field] = int(status)

        try:
            path = path_template.format(
                shop_id=self.settings.pancake_shop_id,
                order_id=normalized_order_id,
                status=int(status),
            )
        except KeyError as exc:
            raise ValidationError(f"Cấu hình update endpoint Pancake thiếu placeholder: {exc}") from exc

        return self._request(method, path, data=payload)

    def get_order_detail(
        self,
        order_id: str,
        *,
        path_template: str = "/shops/{shop_id}/orders/{order_id}",
    ) -> dict[str, Any]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        normalized_order_id = str(order_id).strip()
        if not normalized_order_id:
            raise ValidationError("Thiếu order_id để lấy chi tiết đơn Pancake.")

        path = self._format_order_path(path_template, normalized_order_id)
        payload = self._request("GET", path)
        order = self._extract_order_payload(payload)
        if not isinstance(order, dict):
            raise PancakeApiError(
                f"Không lấy được payload chi tiết đơn Pancake cho order_id={normalized_order_id}."
            )
        return order

    def update_order_note_print(
        self,
        order_id: str,
        note_text: str,
        *,
        update_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.settings.pancake_shop_id <= 0:
            raise ValidationError("Chưa có PANCAKE_SHOP_ID hợp lệ.")
        normalized_order_id = str(order_id).strip()
        if not normalized_order_id:
            raise ValidationError("Thiếu order_id để cập nhật ghi chú in Pancake.")

        cfg = update_cfg if isinstance(update_cfg, dict) else {}
        method = str(cfg.get("method", "PUT")).strip().upper() or "PUT"
        path_template = str(cfg.get("path", "/shops/{shop_id}/orders/{order_id}")).strip()
        note_print_field = str(cfg.get("note_print_field", "note_print")).strip() or "note_print"
        mirror_note_field = str(cfg.get("mirror_note_field", "")).strip()
        safe_full_order_update = self._to_bool(cfg.get("safe_full_order_update"), default=True)
        strict_note_only = self._to_bool(cfg.get("strict_note_only"), default=True)
        verify_after_update = self._to_bool(cfg.get("verify_after_update"), default=True)
        verify_unchanged_fields = self._as_list(cfg.get("verify_unchanged_fields")) or [
            "__items_signature__",
            "total_price",
            "total_quantity",
            "status",
            "is_empty_cart",
        ]
        extra_payload = cfg.get("extra_payload", {})
        path = self._format_order_path(path_template, normalized_order_id)
        normalized_note_text = str(note_text or "").strip()

        if safe_full_order_update:
            source_order = self.get_order_detail(
                normalized_order_id,
                path_template=path_template,
            )
            payload = copy.deepcopy(source_order)
            self._set_nested_value(payload, note_print_field, normalized_note_text)
            if mirror_note_field:
                self._set_nested_value(payload, mirror_note_field, normalized_note_text)

            if strict_note_only:
                allowed_fields = [note_print_field]
                if mirror_note_field:
                    allowed_fields.append(mirror_note_field)
                self._assert_only_allowed_field_changes(
                    source=source_order,
                    target=payload,
                    allowed_paths=allowed_fields,
                )

            if isinstance(extra_payload, dict) and extra_payload:
                # only allow extra fields when they are explicitly part of allowed note paths
                for key in extra_payload.keys():
                    if str(key).strip() not in {note_print_field, mirror_note_field}:
                        raise ValidationError(
                            "Cấu hình update ghi chú Pancake có extra_payload không an toàn. "
                            "Chỉ được sửa trường note_print."
                        )
                payload.update(extra_payload)

            guard_before = self._capture_paths(source_order, verify_unchanged_fields)
            result = self._request(method, path, data=payload)
            if verify_after_update:
                latest_order = self.get_order_detail(
                    normalized_order_id,
                    path_template=path_template,
                )
                guard_after = self._capture_paths(latest_order, verify_unchanged_fields)
                if guard_before != guard_after:
                    raise PancakeApiError(
                        "Cập nhật ghi chú in đã làm thay đổi dữ liệu đơn ngoài phạm vi cho phép; "
                        "đã chặn để tránh tác động thêm."
                    )
            return result

        payload: dict[str, Any] = {}
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        payload[note_print_field] = normalized_note_text
        if mirror_note_field:
            payload[mirror_note_field] = normalized_note_text
        return self._request(method, path, data=payload)

    def _format_order_path(self, path_template: str, order_id: str) -> str:
        try:
            return path_template.format(
                shop_id=self.settings.pancake_shop_id,
                order_id=order_id,
            )
        except KeyError as exc:
            raise ValidationError(f"Cấu hình update ghi chú Pancake thiếu placeholder: {exc}") from exc

    def _extract_order_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload.get("order"), dict):
            return dict(payload.get("order") or {})
        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("order"), dict):
                return dict(data.get("order") or {})
            return dict(data)
        if isinstance(payload.get("result"), dict):
            return dict(payload.get("result") or {})
        return dict(payload)

    def _capture_paths(self, payload: dict[str, Any], paths: list[str]) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        for raw_path in paths:
            path = str(raw_path or "").strip()
            if not path:
                continue
            if path == "__items_signature__":
                snapshot[path] = self._build_items_signature(payload)
                continue
            snapshot[path] = copy.deepcopy(self._get_nested_value(payload, path))
        return snapshot

    def _build_items_signature(self, payload: dict[str, Any]) -> list[tuple[str, int]]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        signature: list[tuple[str, int]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            variation_info = item.get("variation_info")
            if not isinstance(variation_info, dict):
                variation_info = {}
            sku = str(
                variation_info.get("sku")
                or variation_info.get("custom_id")
                or variation_info.get("id")
                or item.get("sku")
                or item.get("variation_id")
                or item.get("product_id")
                or ""
            ).strip()
            quantity = self._to_int(item.get("quantity"), fallback=0)
            signature.append((sku, max(0, quantity)))
        signature.sort(key=lambda row: (row[0], row[1]))
        return signature

    def _assert_only_allowed_field_changes(
        self,
        *,
        source: dict[str, Any],
        target: dict[str, Any],
        allowed_paths: list[str],
    ) -> None:
        src = copy.deepcopy(source)
        dst = copy.deepcopy(target)
        for path in allowed_paths:
            normalized = str(path or "").strip()
            if not normalized:
                continue
            self._remove_nested_value(src, normalized)
            self._remove_nested_value(dst, normalized)
        if src != dst:
            raise ValidationError("Payload cập nhật ghi chú in đang làm thay đổi field ngoài note_print.")

    @staticmethod
    def _split_path(path: str) -> list[str]:
        normalized = str(path or "").replace("[", ".").replace("]", "")
        return [part.strip() for part in normalized.split(".") if part.strip()]

    def _set_nested_value(self, payload: dict[str, Any], path: str, value: Any) -> None:
        parts = self._split_path(path)
        if not parts:
            return
        current: dict[str, Any] = payload
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[parts[-1]] = value

    def _remove_nested_value(self, payload: dict[str, Any], path: str) -> None:
        parts = self._split_path(path)
        if not parts:
            return
        current: Any = payload
        for part in parts[:-1]:
            if not isinstance(current, dict):
                return
            current = current.get(part)
            if current is None:
                return
        if isinstance(current, dict):
            current.pop(parts[-1], None)

    def _get_nested_value(self, payload: dict[str, Any], path: str) -> Any:
        parts = self._split_path(path)
        current: Any = payload
        for part in parts:
            if not isinstance(current, dict):
                return None
            if part not in current:
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _to_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        normalized = str(value).strip().lower()
        if not normalized:
            return default
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        return default

    def _filter_orders_by_local_date(
        self,
        orders: list[dict[str, Any]],
        report_date: date,
        timezone_name: str,
    ) -> list[dict[str, Any]]:
        tzinfo = self._resolve_timezone(timezone_name)
        filtered: list[dict[str, Any]] = []
        for order in orders:
            inserted_at = str(order.get("inserted_at", "")).strip()
            if not inserted_at:
                filtered.append(order)
                continue
            order_dt = self._parse_datetime(inserted_at)
            if not order_dt:
                filtered.append(order)
                continue
            if order_dt.astimezone(tzinfo).date() == report_date:
                filtered.append(order)
        return filtered

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        raw = str(value).strip()
        if not raw:
            return None
        candidate = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _to_unix_day_range(self, report_date: date, timezone_name: str) -> tuple[int, int]:
        tzinfo = self._resolve_timezone(timezone_name)
        day_start = datetime(
            year=report_date.year,
            month=report_date.month,
            day=report_date.day,
            tzinfo=tzinfo,
        )
        day_end = day_start + timedelta(days=1, seconds=-1)
        return int(day_start.timestamp()), int(day_end.timestamp())

    @staticmethod
    def _to_int(value: Any, *, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _resolve_timezone(timezone_name: str) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))
