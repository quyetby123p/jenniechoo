from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.utils import load_json, now_utc_iso


class WebReportService:
    """Aggregate order/reporting data for the web dashboard."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        pancake_client: PancakePosClient,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.pancake = pancake_client
        self._cache_lock = Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def get_snapshot(self, report_date: date) -> dict[str, Any]:
        key = report_date.isoformat()
        now_ts = time.time()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_ts, payload = cached
                if now_ts - cached_ts < float(self.settings.web_report_refresh_seconds):
                    return payload

        payload = self._build_snapshot(report_date)
        with self._cache_lock:
            self._cache[key] = (time.time(), payload)
        return payload

    def _build_snapshot(self, report_date: date) -> dict[str, Any]:
        tz = self._resolve_timezone()
        waiting_cfg = self._load_status_map_config()
        waiting_codes = self._to_int_set(waiting_cfg.get("waiting_status_codes", []))
        waiting_labels = self._to_text_set(waiting_cfg.get("waiting_status_labels", ["chờ hàng", "cho hang"]))
        brand_rules = waiting_cfg.get("brand_rules", [])

        orders = self.pancake.fetch_all_orders_for_date(report_date, self.settings.app_timezone)
        if not isinstance(orders, list):
            orders = []
        orders = [item for item in orders if isinstance(item, dict)]

        total_orders = 0
        closed_orders = 0
        waiting_orders_count = 0
        waiting_value_minor = 0
        missing_line_count = 0
        missing_quantity = 0
        size_totals: dict[str, int] = {}
        missing_products: set[str] = set()

        waiting_orders: list[dict[str, Any]] = []
        brands: dict[str, dict[str, Any]] = {}

        for order in orders:
            total_orders += 1
            closed_orders += 1

            order_ref = self._extract_order_ref(order)
            order_created_dt = self._extract_order_datetime(order, tz=tz)
            order_status_code = self._extract_status_code(order)
            order_status_label = self._extract_status_label(order)
            order_total_minor = self._extract_order_total_minor(order)
            brand_name, brand_slug = self._extract_brand(order, brand_rules=brand_rules)

            brand_bucket = brands.setdefault(
                brand_slug,
                {
                    "brand_name": brand_name,
                    "brand_slug": brand_slug,
                    "total_orders": 0,
                    "closed_orders": 0,
                    "waiting_orders": 0,
                    "waiting_value_minor": 0,
                    "missing_line_count": 0,
                    "missing_quantity": 0,
                    "size_totals": {},
                    "sku_rows": {},
                },
            )
            brand_bucket["total_orders"] = self._to_int(brand_bucket.get("total_orders")) + 1
            brand_bucket["closed_orders"] = self._to_int(brand_bucket.get("closed_orders")) + 1

            is_waiting = self._is_waiting_status(
                code=order_status_code,
                label=order_status_label,
                waiting_codes=waiting_codes,
                waiting_labels=waiting_labels,
            )
            if not is_waiting:
                continue

            waiting_orders_count += 1
            waiting_value_minor += order_total_minor
            brand_bucket["waiting_orders"] = self._to_int(brand_bucket.get("waiting_orders")) + 1
            brand_bucket["waiting_value_minor"] = self._to_int(brand_bucket.get("waiting_value_minor")) + order_total_minor

            order_items = order.get("items", [])
            if not isinstance(order_items, list):
                order_items = []

            line_skus: list[str] = []
            for item in order_items:
                if not isinstance(item, dict):
                    continue
                quantity = max(1, self._to_int(item.get("quantity"), fallback=1))
                size = self._extract_item_size(item)
                color = self._extract_item_color(item)
                sku = self._extract_item_sku(item)
                unit_price_minor = self._extract_item_unit_price_minor(item)
                line_value_minor = max(0, quantity * unit_price_minor)
                normalized_sku = sku or "UNKNOWN"
                line_skus.append(normalized_sku)
                missing_products.add(normalized_sku)

                missing_line_count += 1
                missing_quantity += quantity
                size_totals[size] = self._to_int(size_totals.get(size)) + quantity

                brand_bucket["missing_line_count"] = self._to_int(brand_bucket.get("missing_line_count")) + 1
                brand_bucket["missing_quantity"] = self._to_int(brand_bucket.get("missing_quantity")) + quantity
                brand_sizes = brand_bucket.get("size_totals")
                if not isinstance(brand_sizes, dict):
                    brand_sizes = {}
                    brand_bucket["size_totals"] = brand_sizes
                brand_sizes[size] = self._to_int(brand_sizes.get(size)) + quantity

                row_key = f"{normalized_sku}|{color}"
                sku_rows = brand_bucket.get("sku_rows")
                if not isinstance(sku_rows, dict):
                    sku_rows = {}
                    brand_bucket["sku_rows"] = sku_rows
                row = sku_rows.get(row_key)
                if not isinstance(row, dict):
                    row = {
                        "sku": normalized_sku,
                        "color": color,
                        "sizes": {},
                        "missing_line_count": 0,
                        "missing_quantity": 0,
                        "value_minor": 0,
                        "order_refs": [],
                    }
                    sku_rows[row_key] = row
                row["missing_line_count"] = self._to_int(row.get("missing_line_count")) + 1
                row["missing_quantity"] = self._to_int(row.get("missing_quantity")) + quantity
                row["value_minor"] = self._to_int(row.get("value_minor")) + line_value_minor
                row_sizes = row.get("sizes")
                if not isinstance(row_sizes, dict):
                    row_sizes = {}
                    row["sizes"] = row_sizes
                row_sizes[size] = self._to_int(row_sizes.get(size)) + quantity
                refs = row.get("order_refs")
                if not isinstance(refs, list):
                    refs = []
                    row["order_refs"] = refs
                if order_ref and order_ref not in refs:
                    refs.append(order_ref)

            waiting_orders.append(
                {
                    "order_ref": order_ref,
                    "brand_name": brand_name,
                    "brand_slug": brand_slug,
                    "status_code": order_status_code,
                    "status_label": order_status_label,
                    "created_at": self._format_dt(order_created_dt, tz=tz),
                    "created_ts": self._to_ts(order_created_dt),
                    "order_total_minor": order_total_minor,
                    "order_total_text": self._fmt_currency(order_total_minor),
                    "item_count": len(order_items),
                    "missing_skus": sorted(set(line_skus)),
                }
            )

        waiting_orders.sort(key=lambda item: self._to_int(item.get("created_ts")), reverse=True)

        pending_reconcile = self._load_pending_reconcile_records(report_date)
        pending_reconcile_orders = len(
            {
                self._normalize_text(str(item.get("pancake_order_ref", "")).strip())
                for item in pending_reconcile
                if str(item.get("pancake_order_ref", "")).strip()
            }
        )

        if pending_reconcile_orders <= 0:
            pending_reconcile_orders = len(pending_reconcile)

        brand_overview: list[dict[str, Any]] = []
        brand_detail: dict[str, Any] = {}
        for bucket in sorted(brands.values(), key=lambda item: str(item.get("brand_name", "")).lower()):
            size_summary = self._serialize_size_totals(bucket.get("size_totals"))
            sku_rows_obj = bucket.get("sku_rows")
            sku_rows: list[dict[str, Any]] = []
            if isinstance(sku_rows_obj, dict):
                sku_rows = sorted(
                    (
                        {
                            "sku": str(row.get("sku", "")).strip() or "UNKNOWN",
                            "color": str(row.get("color", "")).strip() or "Khác",
                            "sizes": self._normalize_size_map(row.get("sizes")),
                            "missing_line_count": self._to_int(row.get("missing_line_count")),
                            "missing_quantity": self._to_int(row.get("missing_quantity")),
                            "value_minor": self._to_int(row.get("value_minor")),
                            "value_text": self._fmt_currency(self._to_int(row.get("value_minor"))),
                            "order_refs": sorted(
                                {
                                    str(ref).strip()
                                    for ref in (row.get("order_refs") if isinstance(row.get("order_refs"), list) else [])
                                    if str(ref).strip()
                                }
                            ),
                        }
                        for row in sku_rows_obj.values()
                        if isinstance(row, dict)
                    ),
                    key=lambda row: (self._to_int(row.get("missing_quantity")), str(row.get("sku", ""))),
                    reverse=True,
                )
            brand_record = {
                "brand_name": str(bucket.get("brand_name", "")).strip() or "Khác",
                "brand_slug": str(bucket.get("brand_slug", "")).strip() or "khac",
                "total_orders": self._to_int(bucket.get("total_orders")),
                "closed_orders": self._to_int(bucket.get("closed_orders")),
                "waiting_orders": self._to_int(bucket.get("waiting_orders")),
                "waiting_value_minor": self._to_int(bucket.get("waiting_value_minor")),
                "waiting_value_text": self._fmt_currency(self._to_int(bucket.get("waiting_value_minor"))),
                "missing_line_count": self._to_int(bucket.get("missing_line_count")),
                "missing_quantity": self._to_int(bucket.get("missing_quantity")),
                "size_summary": size_summary,
                "top_skus": sku_rows[:10],
            }
            brand_overview.append(brand_record)
            brand_detail[brand_record["brand_slug"]] = {
                **brand_record,
                "sku_rows": sku_rows,
            }

        payload = {
            "ok": True,
            "report_date": report_date.isoformat(),
            "timezone": self.settings.app_timezone,
            "generated_at": now_utc_iso(),
            "metrics": {
                "total_orders": total_orders,
                "closed_orders": closed_orders,
                "waiting_orders": waiting_orders_count,
                "pending_reconcile_orders": pending_reconcile_orders,
                "missing_line_count": missing_line_count,
                "missing_quantity": missing_quantity,
                "missing_product_count": len(missing_products),
                "waiting_value_minor": waiting_value_minor,
                "waiting_value_text": self._fmt_currency(waiting_value_minor),
            },
            "size_summary": self._serialize_size_totals(size_totals),
            "brands": brand_overview,
            "brand_detail": brand_detail,
            "status_lists": {
                "waiting": waiting_orders,
                "pending-reconcile": pending_reconcile,
            },
        }
        return payload

    def _load_pending_reconcile_records(self, report_date: date) -> list[dict[str, Any]]:
        run_dir = self.settings.reconcile_cod_runs_dir
        if not run_dir.exists():
            return []

        pattern = f"run_{report_date.isoformat()}_*.json"
        run_paths = sorted(run_dir.glob(pattern), reverse=True)
        if not run_paths:
            return []

        latest_payload = None
        latest_ts = -1.0
        for path in run_paths:
            payload = self._safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            generated_ts = self._to_generated_ts(payload)
            if generated_ts > latest_ts:
                latest_ts = generated_ts
                latest_payload = payload
        if not isinstance(latest_payload, dict):
            return []

        records = latest_payload.get("records", [])
        if not isinstance(records, list):
            return []

        pending_states = {"not_found", "ambiguous", "unmapped_status"}
        output: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            result = self._normalize_text(str(record.get("match_result", "")).strip())
            if result not in pending_states:
                continue
            ref = str(record.get("pancake_display_id", "")).strip() or str(record.get("pancake_order_id", "")).strip()
            output.append(
                {
                    "pancake_order_ref": ref,
                    "match_result": result,
                    "reason": str(record.get("reason", "")).strip(),
                    "td_awb": str(record.get("td_awb", "")).strip(),
                    "td_status": str(record.get("td_status", "")).strip(),
                    "customer_name": str(record.get("td_customer_name", "")).strip(),
                    "settlement_date": str(record.get("settlement_date", "")).strip(),
                }
            )
        output.sort(key=lambda item: str(item.get("pancake_order_ref", "")))
        return output

    def _extract_brand(self, order: dict[str, Any], *, brand_rules: Any) -> tuple[str, str]:
        candidate_fields = (
            "brand_name",
            "brand",
            "shop_name",
            "page_name",
            "source_name",
            "store_name",
        )
        for field in candidate_fields:
            value = str(order.get(field, "")).strip()
            if value:
                return value, self._slugify(value)

        item_codes: list[str] = []
        items = order.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sku = self._extract_item_sku(item)
                if sku:
                    item_codes.append(sku)
        order_ref = self._extract_order_ref(order)
        if order_ref:
            item_codes.append(order_ref)

        normalized_codes = [self._normalize_text(code) for code in item_codes if code]
        if isinstance(brand_rules, list):
            for rule in brand_rules:
                if not isinstance(rule, dict):
                    continue
                pattern = str(rule.get("pattern", "")).strip()
                brand_name = str(rule.get("brand_name", "")).strip()
                if not pattern or not brand_name:
                    continue
                try:
                    regex = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    continue
                for code in normalized_codes:
                    if regex.search(code):
                        slug = str(rule.get("brand_slug", "")).strip() or self._slugify(brand_name)
                        return brand_name, slug

        for code in normalized_codes:
            if code.startswith("jc"):
                return "Jennie Choo", "jennie-choo"
            if code.startswith("lys") or code.startswith("l-") or code.startswith("l "):
                return "Lysilk", "lysilk"
            if code.startswith("sa") or code.startswith("s-") or code.startswith("s "):
                return "Say Studios", "say-studios"
        return "Khác", "khac"

    def _extract_order_ref(self, order: dict[str, Any]) -> str:
        candidates = (
            order.get("display_id"),
            order.get("code"),
            order.get("order_code"),
            order.get("order_id"),
            order.get("id"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def _extract_status_code(self, order: dict[str, Any]) -> int | None:
        raw = order.get("status")
        if isinstance(raw, bool):
            return None
        try:
            if raw is None:
                return None
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None

    def _extract_status_label(self, order: dict[str, Any]) -> str:
        for key in ("status_name", "status_text", "status_label", "order_status_name"):
            value = str(order.get(key, "")).strip()
            if value:
                return value
        raw = order.get("status")
        if isinstance(raw, str):
            return raw.strip()
        return ""

    def _is_waiting_status(
        self,
        *,
        code: int | None,
        label: str,
        waiting_codes: set[int],
        waiting_labels: set[str],
    ) -> bool:
        if code is not None and code in waiting_codes:
            return True
        normalized_label = self._normalize_text(label)
        if normalized_label and normalized_label in waiting_labels:
            return True
        if "cho hang" in normalized_label:
            return True
        return False

    def _extract_order_total_minor(self, order: dict[str, Any]) -> int:
        for key in ("total_price", "total", "amount_total"):
            value = order.get(key)
            if value is None:
                continue
            return self._to_int(value)
        return 0

    def _extract_item_sku(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        candidates = (
            item.get("sku"),
            item.get("display_id"),
            variation_info.get("display_id"),
            variation_info.get("product_id"),
            variation_info.get("sku"),
            variation_info.get("name"),
            item.get("name"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return "UNKNOWN"

    def _extract_item_color(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        for key in ("color", "colour"):
            value = str(variation_info.get(key, "")).strip()
            if value:
                return value
        fields = variation_info.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = self._normalize_text(str(field.get("name", "")).strip())
                if "mau" not in name and "color" not in name and "colour" not in name:
                    continue
                value = str(field.get("value", "")).strip()
                if value:
                    return value
        variation_name = str(variation_info.get("name", "")).strip()
        if variation_name:
            parts = [part.strip() for part in re.split(r"[-|/]", variation_name) if part.strip()]
            if len(parts) >= 2:
                return parts[-2]
        return "Khác"

    def _extract_item_size(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        for key in ("size",):
            value = str(variation_info.get(key, "")).strip()
            if value:
                return value.upper()
        fields = variation_info.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = self._normalize_text(str(field.get("name", "")).strip())
                if "size" not in name and "kich co" not in name:
                    continue
                value = str(field.get("value", "")).strip()
                if value:
                    return value.upper()
        candidates = [
            str(item.get("name", "")).strip(),
            str(variation_info.get("name", "")).strip(),
            str(self._extract_item_sku(item)).strip(),
        ]
        size_pattern = re.compile(r"\b(XXXL|XXL|XL|XS|S|M|L|2XL|3XL|4XL)\b", re.IGNORECASE)
        for candidate in candidates:
            match = size_pattern.search(candidate)
            if match:
                return match.group(1).upper()
        return "KHÁC"

    def _extract_item_unit_price_minor(self, item: dict[str, Any]) -> int:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        for key in ("retail_price", "price", "sale_price"):
            raw = variation_info.get(key, item.get(key))
            if raw is None:
                continue
            return max(0, self._to_int(raw))
        return 0

    def _extract_order_datetime(self, order: dict[str, Any], *, tz: timezone | ZoneInfo) -> datetime | None:
        ts_candidates = (
            order.get("inserted_at"),
            order.get("created_at"),
            order.get("created_time"),
            order.get("createdAt"),
            order.get("insertedAt"),
        )
        dt = None
        for raw in ts_candidates:
            dt = self._parse_datetime(raw)
            if dt is not None:
                break
        if dt is None:
            unix_candidates = (
                order.get("inserted_time"),
                order.get("created"),
                order.get("create_time"),
                order.get("created_timestamp"),
            )
            for raw in unix_candidates:
                dt = self._parse_unix_datetime(raw)
                if dt is not None:
                    break
        if dt is None:
            return None
        return dt.astimezone(tz)

    def _load_status_map_config(self) -> dict[str, Any]:
        path = self.settings.web_report_status_map_config_path
        if not path.exists():
            return self._default_status_map_config()
        payload = self._safe_read_json(path)
        if not isinstance(payload, dict):
            return self._default_status_map_config()
        defaults = self._default_status_map_config()
        merged = {**defaults, **payload}
        if not isinstance(merged.get("brand_rules"), list):
            merged["brand_rules"] = defaults.get("brand_rules", [])
        return merged

    def _default_status_map_config(self) -> dict[str, Any]:
        return {
            "waiting_status_codes": [],
            "waiting_status_labels": ["chờ hàng", "cho hang", "waiting"],
            "brand_rules": [
                {"pattern": r"^JC", "brand_name": "Jennie Choo", "brand_slug": "jennie-choo"},
                {"pattern": r"^(LYS|L-)", "brand_name": "Lysilk", "brand_slug": "lysilk"},
                {"pattern": r"^(SA|S-)", "brand_name": "Say Studios", "brand_slug": "say-studios"},
            ],
        }

    @staticmethod
    def _safe_read_json(path: Path) -> Any:
        try:
            return load_json(path)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            if value is None:
                return fallback
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _to_generated_ts(payload: dict[str, Any]) -> float:
        raw = str(payload.get("generated_at", "")).strip()
        dt = WebReportService._parse_datetime(raw)
        if dt is None:
            return 0.0
        return dt.timestamp()

    @staticmethod
    def _to_ts(value: datetime | None) -> int:
        if value is None:
            return 0
        return int(value.timestamp())

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        candidate = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _parse_unix_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            numeric = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if numeric > 2_000_000_000_000:
            numeric = numeric / 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)

    @staticmethod
    def _normalize_text(raw: str) -> str:
        text = str(raw or "").strip().lower()
        if not text:
            return ""
        replacements = {
            "à": "a",
            "á": "a",
            "ạ": "a",
            "ả": "a",
            "ã": "a",
            "â": "a",
            "ầ": "a",
            "ấ": "a",
            "ậ": "a",
            "ẩ": "a",
            "ẫ": "a",
            "ă": "a",
            "ằ": "a",
            "ắ": "a",
            "ặ": "a",
            "ẳ": "a",
            "ẵ": "a",
            "è": "e",
            "é": "e",
            "ẹ": "e",
            "ẻ": "e",
            "ẽ": "e",
            "ê": "e",
            "ề": "e",
            "ế": "e",
            "ệ": "e",
            "ể": "e",
            "ễ": "e",
            "ì": "i",
            "í": "i",
            "ị": "i",
            "ỉ": "i",
            "ĩ": "i",
            "ò": "o",
            "ó": "o",
            "ọ": "o",
            "ỏ": "o",
            "õ": "o",
            "ô": "o",
            "ồ": "o",
            "ố": "o",
            "ộ": "o",
            "ổ": "o",
            "ỗ": "o",
            "ơ": "o",
            "ờ": "o",
            "ớ": "o",
            "ợ": "o",
            "ở": "o",
            "ỡ": "o",
            "ù": "u",
            "ú": "u",
            "ụ": "u",
            "ủ": "u",
            "ũ": "u",
            "ư": "u",
            "ừ": "u",
            "ứ": "u",
            "ự": "u",
            "ử": "u",
            "ữ": "u",
            "ỳ": "y",
            "ý": "y",
            "ỵ": "y",
            "ỷ": "y",
            "ỹ": "y",
            "đ": "d",
        }
        for src, dest in replacements.items():
            text = text.replace(src, dest)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = WebReportService._normalize_text(value)
        normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        return normalized or "khac"

    def _fmt_currency(self, minor_value: int) -> str:
        factor = max(1, int(self.settings.report_thb_minor_unit_factor))
        major = float(minor_value) / float(factor)
        formatted = f"{major:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        if formatted.endswith(",00"):
            formatted = formatted[:-3]
        return f"{formatted}đ"

    @staticmethod
    def _format_dt(value: datetime | None, *, tz: timezone | ZoneInfo) -> str:
        if value is None:
            return ""
        return value.astimezone(tz).strftime("%H:%M %d-%m-%Y")

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone.utc

    @staticmethod
    def _to_int_set(values: Any) -> set[int]:
        result: set[int] = set()
        if not isinstance(values, list):
            return result
        for item in values:
            try:
                result.add(int(str(item).strip()))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _to_text_set(values: Any) -> set[str]:
        result: set[str] = set()
        if not isinstance(values, list):
            return result
        for item in values:
            text = WebReportService._normalize_text(str(item).strip())
            if text:
                result.add(text)
        return result

    @staticmethod
    def _serialize_size_totals(size_map: Any) -> list[dict[str, Any]]:
        normalized = WebReportService._normalize_size_map(size_map)
        return [
            {"size": size, "quantity": quantity}
            for size, quantity in sorted(
                normalized.items(),
                key=lambda item: (item[0] == "KHÁC", item[0]),
            )
        ]

    @staticmethod
    def _normalize_size_map(size_map: Any) -> dict[str, int]:
        if not isinstance(size_map, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in size_map.items():
            size = str(key or "").strip().upper() or "KHÁC"
            result[size] = WebReportService._to_int(result.get(size)) + WebReportService._to_int(value)
        return result
