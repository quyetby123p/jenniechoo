from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.utils import dump_json, now_utc_iso


class DailyReportService:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        pancake_client: PancakePosClient,
        meta_client: MetaAdsClient,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.pancake = pancake_client
        self.meta = meta_client
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.settings.reports_daily_dir.mkdir(parents=True, exist_ok=True)
        self.settings.reports_error_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(self, report_date: date | None = None) -> dict[str, Any]:
        target_date = report_date or self.default_report_date()
        errors: dict[str, str] = {}
        warnings: list[str] = []
        pos_summary: dict[str, Any] | None = None
        ads_summary: dict[str, Any] | None = None
        top_products: list[dict[str, Any]] = []

        try:
            orders: list[dict[str, Any]] = []
            pos_aggs: dict[str, Any] = {}
            if hasattr(self.pancake, "fetch_daily_orders_snapshot"):
                snapshot = self.pancake.fetch_daily_orders_snapshot(
                    target_date,
                    self.settings.app_timezone,
                )
                if isinstance(snapshot, dict):
                    raw_orders = snapshot.get("orders")
                    if isinstance(raw_orders, list):
                        orders = [item for item in raw_orders if isinstance(item, dict)]
                    raw_aggs = snapshot.get("aggs")
                    if isinstance(raw_aggs, dict):
                        pos_aggs = raw_aggs
            if not orders and not pos_aggs:
                orders = self.pancake.fetch_all_orders_for_date(
                    target_date,
                    self.settings.app_timezone,
                )
            pos_summary, top_products, agg_warnings = self._aggregate_pos(orders, pos_aggs=pos_aggs)
            warnings.extend(agg_warnings)
        except Exception as exc:  # noqa: BLE001
            errors["pos"] = str(exc)
            self.logger.exception("Lay du lieu POS that bai cho %s", target_date.isoformat())

        try:
            ads_summary = self.meta.get_daily_spend(target_date, self.settings.app_timezone)
        except Exception as exc:  # noqa: BLE001
            errors["ads"] = str(exc)
            self.logger.exception("Lay chi phi Ads that bai cho %s", target_date.isoformat())

        if "pos" in errors:
            warnings.append(f"Không lấy được dữ liệu POS: {errors['pos']}")
        if "ads" in errors:
            warnings.append(f"Không lấy được dữ liệu Ads: {errors['ads']}")

        report: dict[str, Any] = {
            "ok": not errors,
            "partial": bool(errors) and (pos_summary is not None or ads_summary is not None),
            "report_date": target_date.isoformat(),
            "timezone": self.settings.app_timezone,
            "generated_at": now_utc_iso(),
            "pos": pos_summary,
            "ads": ads_summary,
            "top_products": top_products,
            "warnings": warnings,
            "errors": errors,
        }

        report["roas"] = self._calculate_roas(report)
        self._save_report(report)
        self._cleanup_old_reports(reference_date=target_date)
        return report

    def build_message(self, report: dict[str, Any], trigger_label: str = "") -> str:
        tzinfo = self._resolve_timezone()
        report_date = self._parse_report_date(str(report.get("report_date", "")))
        generated_at_text = self._format_generated_at(str(report.get("generated_at", "")), tzinfo)

        lines: list[str] = []
        if trigger_label:
            lines.append(trigger_label)
        lines.extend(
            [
                f"Báo cáo ngày {report_date.strftime('%d/%m/%Y')} ({self.settings.app_timezone})",
                f"Thời gian tạo: {generated_at_text}",
                f"Tổng quan: {self._status_text(report)}",
                "",
            ]
        )

        pos = report.get("pos") if isinstance(report.get("pos"), dict) else None
        if pos:
            lines.append(f"Doanh thu POS: {self._fmt_thb(self._to_float(pos.get('revenue_total_thb')))}")
            lines.append(
                "Quy đổi VND: "
                f"{self._fmt_vnd(self._to_int(pos.get('revenue_total_vnd')))} "
                f"(1 THB = {self._fmt_rate(self._to_float(pos.get('thb_to_vnd_rate')))} VND)"
            )
            lines.append(f"Số đơn POS: {self._to_int(pos.get('order_count')):,}")
            lines.append(f"Tổng số lượng sản phẩm: {self._to_int(pos.get('quantity_total')):,}")
        else:
            lines.append("Doanh thu POS: chưa có dữ liệu.")

        ads = report.get("ads") if isinstance(report.get("ads"), dict) else None
        if ads:
            lines.append(f"Chi phí Ads: {self._fmt_vnd(self._to_int(ads.get('spend_vnd')))}")
        else:
            lines.append("Chi phí Ads: chưa có dữ liệu.")

        roas = report.get("roas")
        if isinstance(roas, (int, float)) and roas > 0:
            lines.append(f"ROAS: {roas:.2f}")
        lines.append("")

        products = report.get("top_products") if isinstance(report.get("top_products"), list) else []
        if products:
            lines.append("Top 10 sản phẩm theo doanh thu ước tính (THB):")
            for idx, product in enumerate(products, start=1):
                if idx > 10:
                    break
                name = str(product.get("name", "")).strip() or "Không tên"
                quantity = self._to_int(product.get("quantity"))
                revenue_thb = self._to_float(product.get("revenue_thb"))
                revenue_vnd = self._to_int(product.get("revenue_vnd"))
                lines.append(
                    f"{idx}) {name} | SL: {quantity:,} | DT: {self._fmt_thb(revenue_thb)} "
                    f"(~{self._fmt_vnd(revenue_vnd)})"
                )
        else:
            lines.append("Top 10 sản phẩm: chưa có dữ liệu.")

        warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
        if warnings:
            lines.append("")
            lines.append("Cảnh báo:")
            for warning in warnings:
                lines.append(f"- {warning}")

        return "\n".join(lines)

    def default_report_date(self) -> date:
        now_local = datetime.now(self._resolve_timezone())
        return (now_local - timedelta(days=1)).date()

    def _aggregate_pos(
        self,
        orders: list[dict[str, Any]],
        *,
        pos_aggs: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        rate = max(0.0, float(self.settings.report_thb_to_vnd_rate))
        thb_minor_factor = max(1, int(self.settings.report_thb_minor_unit_factor))
        revenue_total_thb = 0.0
        has_revenue_from_aggs = False
        order_count = 0
        quantity_total = 0
        product_map: dict[str, dict[str, Any]] = {}
        non_thb_totals: dict[str, int] = {}
        warnings: list[str] = []

        revenue_minor_from_aggs = self._extract_revenue_minor_from_aggs(pos_aggs, fallback=None)
        if revenue_minor_from_aggs is not None:
            revenue_total_thb = self._minor_to_major(revenue_minor_from_aggs, thb_minor_factor)
            has_revenue_from_aggs = True

        for order in orders:
            order_count += 1
            order_total = self._to_int(order.get("total_price"))
            currency = self._normalize_currency(order.get("order_currency"))
            if currency == "THB":
                if not has_revenue_from_aggs:
                    revenue_total_thb += self._minor_to_major(order_total, thb_minor_factor)
            else:
                non_thb_totals[currency] = self._to_int(non_thb_totals.get(currency, 0)) + order_total

            items = order.get("items", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                quantity = max(0, self._to_int(item.get("quantity")))
                quantity_total += quantity
                if currency != "THB":
                    continue
                variation_info = item.get("variation_info")
                if not isinstance(variation_info, dict):
                    variation_info = {}
                unit_price_minor = max(0, self._to_int(variation_info.get("retail_price")))
                revenue_item_minor = quantity * unit_price_minor
                revenue_item_thb = self._minor_to_major(revenue_item_minor, thb_minor_factor)
                revenue_item_vnd = int(round(revenue_item_thb * rate))

                variation_id = str(item.get("variation_id", "")).strip()
                product_id = str(variation_info.get("product_id", "")).strip()
                name = str(variation_info.get("name", "")).strip() or "Không tên"
                display_id = str(variation_info.get("display_id", "")).strip()
                key = variation_id or product_id or name

                current = product_map.get(key)
                if not current:
                    current = {
                        "key": key,
                        "variation_id": variation_id,
                        "product_id": product_id,
                        "display_id": display_id,
                        "name": name,
                        "quantity": 0,
                        "revenue_thb": 0.0,
                        "revenue_vnd": 0,
                    }
                    product_map[key] = current
                current["quantity"] = self._to_int(current.get("quantity")) + quantity
                current["revenue_thb"] = self._to_float(current.get("revenue_thb")) + revenue_item_thb
                current["revenue_vnd"] = self._to_int(current.get("revenue_vnd")) + revenue_item_vnd

        top_products = sorted(
            product_map.values(),
            key=lambda item: (
                self._to_float(item.get("revenue_thb")),
                self._to_int(item.get("quantity")),
                str(item.get("name", "")),
            ),
            reverse=True,
        )[:10]

        if not has_revenue_from_aggs:
            warnings.append(
                "POS chưa trả về chỉ số tổng hợp doanh thu; báo cáo đang fallback theo giá trị từng đơn."
            )
        if non_thb_totals:
            detail = ", ".join(
                f"{currency}: {amount:,}"
                for currency, amount in sorted(non_thb_totals.items())
            )
            warnings.append(
                "Có đơn POS không phải THB nên chưa quy đổi trong báo cáo THB: "
                + detail
            )

        summary = {
            "revenue_total_thb": round(revenue_total_thb, 2),
            "revenue_total_vnd": int(round(revenue_total_thb * rate)),
            "thb_to_vnd_rate": rate,
            "thb_minor_unit_factor": thb_minor_factor,
            "order_count": order_count,
            "quantity_total": quantity_total,
        }
        return summary, top_products, warnings

    def _extract_revenue_minor_from_aggs(self, aggs: dict[str, Any] | None, fallback: int | None = 0) -> int | None:
        if not isinstance(aggs, dict):
            return fallback
        cod = self._to_float(self._extract_nested_value(aggs, ["cod", "value"]))
        prepaid = self._to_float(self._extract_nested_value(aggs, ["prepaid", "value"]))
        if cod <= 0 and prepaid <= 0:
            return fallback
        return int(round(cod + prepaid))

    @staticmethod
    def _extract_nested_value(payload: dict[str, Any], path: list[str]) -> Any:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _save_report(self, report: dict[str, Any]) -> Path:
        report_date = str(report.get("report_date", "")).strip()
        target_dir = self.settings.reports_daily_dir if report.get("ok") else self.settings.reports_error_dir
        path = target_dir / f"report_{report_date}.json"
        dump_json(path, report)
        return path

    def _cleanup_old_reports(self, reference_date: date) -> None:
        cutoff_date = reference_date - timedelta(days=max(1, self.settings.daily_report_history_days))
        for root in (self.settings.reports_daily_dir, self.settings.reports_error_dir):
            for path in root.glob("report_*.json"):
                file_date = self._extract_file_date(path.name)
                if file_date and file_date < cutoff_date:
                    try:
                        path.unlink()
                    except Exception:  # noqa: BLE001
                        self.logger.warning("Khong xoa duoc report cu: %s", path)

    @staticmethod
    def _extract_file_date(file_name: str) -> date | None:
        prefix = "report_"
        suffix = ".json"
        if not file_name.startswith(prefix) or not file_name.endswith(suffix):
            return None
        raw = file_name[len(prefix) : -len(suffix)]
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _calculate_roas(self, report: dict[str, Any]) -> float:
        pos = report.get("pos")
        ads = report.get("ads")
        if not isinstance(pos, dict) or not isinstance(ads, dict):
            return 0.0
        revenue = self._to_int(pos.get("revenue_total_vnd"))
        spend = self._to_int(ads.get("spend_vnd"))
        if spend <= 0:
            return 0.0
        return round(revenue / spend, 2)

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    @staticmethod
    def _parse_report_date(raw: str) -> date:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return datetime.now().date()

    @staticmethod
    def _format_generated_at(raw: str, tzinfo: timezone | ZoneInfo) -> str:
        value = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value or "không xác định"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tzinfo).strftime("%d/%m/%Y %H:%M")

    @staticmethod
    def _status_text(report: dict[str, Any]) -> str:
        if report.get("ok"):
            return "OK"
        if report.get("partial"):
            return "CẢNH BÁO (thiếu một phần dữ liệu)"
        return "LỖI"

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fmt_vnd(value: int) -> str:
        return f"{value:,} VND"

    @staticmethod
    def _fmt_thb(value: float) -> str:
        numeric = float(value)
        if abs(numeric - int(numeric)) < 1e-9:
            return f"{int(numeric):,} THB"
        return f"{numeric:,.2f} THB"

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fmt_rate(value: float) -> str:
        if abs(value - int(value)) < 1e-9:
            return f"{int(value):,}"
        return f"{value:,.2f}"

    @staticmethod
    def _normalize_currency(value: Any) -> str:
        text = str(value or "").strip().upper()
        if not text:
            return "UNKNOWN"
        return text

    @staticmethod
    def _minor_to_major(value: int, factor: int) -> float:
        return float(value) / float(max(1, factor))
