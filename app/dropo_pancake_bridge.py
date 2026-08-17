"""Cầu nối Dropo landing -> Pancake POS.

Luồng: khách đặt hàng trên vayxath.com/vxvNNN -> Dropo thu lead -> Dropo đổ
sang Google Sheet tab "Leads" -> service này đọc dòng chưa đồng bộ và tạo đơn
trong Pancake POS, rồi ghi order id ngược lại Sheet.

Vì sao mốc chống trùng nằm ở Sheet chứ không phải file state:
    Service này chạy được cả trên máy local lẫn GitHub Actions / Render, nơi
    ổ đĩa là ephemeral. File state sẽ mất sau mỗi lần chạy và đơn sẽ bị tạo
    trùng. Cột "Pancake Order ID" trong Sheet là nguồn sự thật duy nhất, bền
    qua mọi lần deploy.

Cấu hình theo profile giống phần còn lại của app: biến môi trường có tiền tố
(vd ADS2_) được ưu tiên, không có thì rơi về biến không tiền tố.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

import requests

from app.exceptions import PancakeApiError, ValidationError
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

COL_ORDER_ID = "Pancake Order ID"
COL_SYNC_STATUS = "Sync status"

SKIP_PREFIX = "BỎ QUA"
HANOI_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Bắt mã dạng VXV002-DEN-M / VXV008-XANH MINT-XL, chấp nhận hậu tố lô "-B1".
SKU_PATTERN = re.compile(
    r"VXV\d{3}-[^()·|,]+?-(?:XXL|XL|L|M)(?=-B\d+|[\s)·|,]|$)",
    re.IGNORECASE,
)


def _env(name: str, prefix: str, default: str = "") -> str:
    """Đọc biến môi trường theo profile, cùng quy ước với _profile_env trong settings.

    prefix là tên profile viết hoa KHÔNG kèm gạch dưới (vd "ADS2"); hàm tự nối
    thành ADS2_TEN_BIEN. Không có biến theo profile thì rơi về biến không tiền tố.
    """
    if prefix:
        scoped = os.getenv(f"{prefix}_{name}")
        if scoped is not None and scoped.strip():
            return scoped.strip()
    return (os.getenv(name) or default).strip()


def _env_flag(name: str, prefix: str, default: str = "0") -> bool:
    return _env(name, prefix, default).lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, prefix: str, default: int) -> int:
    raw = _env(name, prefix, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class BridgeConfig:
    enabled: bool = False
    dry_run: bool = True
    spreadsheet_id: str = ""
    sheet_tab: str = "Leads"
    sku_map_path: str = ""
    max_rows: int = 500
    batch_limit: int = 25
    poll_seconds: int = 300
    warehouse_id: str = ""
    order_status: int = 0
    # Pancake lưu giá ở đơn vị nhỏ (minor unit): 849 THB -> 84900.
    # Bảng map SKU đọc từ chính Pancake cho retail_price_minor = 84900 với
    # sản phẩm bán 849 THB, nên hệ số mặc định là 100. Tách ra thành biến môi
    # trường để nếu shop cấu hình khác thì đổi được mà không phải sửa code.
    price_scale: int = 100
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_refresh_token: str = ""
    oauth_token_uri: str = DEFAULT_TOKEN_URI

    @classmethod
    def from_env(cls, prefix: str = "") -> "BridgeConfig":
        return cls(
            enabled=_env_flag("DROPO_PANCAKE_BRIDGE_ENABLED", prefix, "0"),
            dry_run=_env_flag("DROPO_PANCAKE_BRIDGE_DRY_RUN", prefix, "1"),
            spreadsheet_id=_env("DROPO_PANCAKE_SHEET_ID", prefix),
            sheet_tab=_env("DROPO_PANCAKE_SHEET_TAB", prefix, "Leads"),
            sku_map_path=_env("DROPO_PANCAKE_SKU_MAP_PATH", prefix),
            max_rows=_env_int("DROPO_PANCAKE_MAX_ROWS", prefix, 500),
            batch_limit=_env_int("DROPO_PANCAKE_BATCH_LIMIT", prefix, 25),
            poll_seconds=_env_int("DROPO_PANCAKE_POLL_SECONDS", prefix, 300),
            warehouse_id=_env("DROPO_PANCAKE_WAREHOUSE_ID", prefix),
            order_status=_env_int("DROPO_PANCAKE_ORDER_STATUS", prefix, 0),
            price_scale=_env_int("DROPO_PANCAKE_PRICE_SCALE", prefix, 100),
            # Dùng chung OAuth Google đã có sẵn của app; ưu tiên biến riêng nếu khai.
            oauth_client_id=(
                _env("DROPO_PANCAKE_OAUTH_CLIENT_ID", prefix)
                or _env("RECONCILE_COD_SHEET_OAUTH_CLIENT_ID", prefix)
                or _env("BOT3_GOOGLE_OAUTH_CLIENT_ID", prefix)
            ),
            oauth_client_secret=(
                _env("DROPO_PANCAKE_OAUTH_CLIENT_SECRET", prefix)
                or _env("RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET", prefix)
                or _env("BOT3_GOOGLE_OAUTH_CLIENT_SECRET", prefix)
            ),
            oauth_refresh_token=(
                _env("DROPO_PANCAKE_OAUTH_REFRESH_TOKEN", prefix)
                or _env("RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN", prefix)
                or _env("BOT3_GOOGLE_OAUTH_REFRESH_TOKEN", prefix)
            ),
            oauth_token_uri=(
                _env("DROPO_PANCAKE_OAUTH_TOKEN_URI", prefix)
                or _env("RECONCILE_COD_SHEET_OAUTH_TOKEN_URI", prefix)
                or DEFAULT_TOKEN_URI
            ),
        )

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.spreadsheet_id:
            missing.append("DROPO_PANCAKE_SHEET_ID")
        if not self.oauth_client_id:
            missing.append("OAUTH_CLIENT_ID")
        if not self.oauth_client_secret:
            missing.append("OAUTH_CLIENT_SECRET")
        if not self.oauth_refresh_token:
            missing.append("OAUTH_REFRESH_TOKEN")
        return missing


@dataclass
class RowResult:
    row_index: int
    status: str  # created | dry_run | skipped | failed
    order_id: str = ""
    message: str = ""


@dataclass
class BridgeReport:
    scanned: int = 0
    pending: int = 0
    created: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    rows: list[RowResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "pending": self.pending,
            "created": self.created,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "failed": self.failed,
            "rows": [
                {
                    "row": r.row_index,
                    "status": r.status,
                    "order_id": r.order_id,
                    "message": r.message,
                }
                for r in self.rows
            ],
        }


class DropoPancakeBridge:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        *,
        config: BridgeConfig | None = None,
        pancake_client: PancakePosClient | None = None,
        session: Any | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.config = config or BridgeConfig.from_env(
            os.getenv("ADS_PROFILE_PREFIX", os.getenv("ADS_PROFILE", "")).strip().upper()
        )
        self.pancake = pancake_client or PancakePosClient(settings, logger)
        self.session = session or requests
        self._sku_map: dict[str, dict[str, Any]] | None = None
        self._pancake_catalog: dict[str, list[dict[str, Any]]] = {}
        self._access_token: str = ""
        self._geo_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # ───────────────────────────── public ─────────────────────────────

    def is_configured(self) -> tuple[bool, str]:
        missing = self.config.missing_fields()
        if missing:
            return False, "Thiếu cấu hình: " + ", ".join(missing)
        if not self.pancake.is_configured():
            return False, "Thiếu PANCAKE_SHOP_ID hoặc PANCAKE_ACCESS_TOKEN."
        if not self.sku_map:
            return False, f"Không đọc được SKU map tại {self.config.sku_map_path!r}."
        return True, ""

    def run_once(self) -> BridgeReport:
        report = BridgeReport()
        ok, reason = self.is_configured()
        if not ok:
            raise ValidationError(reason)

        values = self._fetch_sheet_values()
        if not values:
            return report

        header = [str(c).strip() for c in values[0]]
        columns = self._ensure_tracking_columns(header)
        data_rows = values[1:]
        self._normalize_sheet_timestamps(data_rows, header)
        report.scanned = len(data_rows)

        processed = 0
        for offset, raw_row in enumerate(data_rows):
            row_index = offset + 2  # 1-based, +1 cho header
            row = self._pad(raw_row, len(header))
            existing_id = str(row[columns[COL_ORDER_ID]] or "").strip()
            if existing_id:
                continue
            status_note = str(row[columns[COL_SYNC_STATUS]] or "").strip()
            if status_note.startswith(SKIP_PREFIX):
                continue
            phone = self._clean_phone(
                self._cell(
                    row,
                    header,
                    "เบอร์โทรศัพท์ / Phone",
                    "Số điện thoại",
                    "SĐT",
                    "Điện thoại",
                    "sdt",
                    "c_sdt",
                )
            )
            if not phone:
                continue

            if processed >= self.config.batch_limit:
                self.logger.info(
                    "Đạt batch_limit=%s, còn dòng chờ ở lần chạy sau.", self.config.batch_limit
                )
                break
            report.pending += 1
            processed += 1

            try:
                payload = self.build_order_payload(row, header)
            except ValidationError as exc:
                report.skipped += 1
                report.rows.append(RowResult(row_index, "skipped", message=str(exc)))
                self._write_updates(
                    [self._cell_update(columns[COL_SYNC_STATUS], row_index, f"{SKIP_PREFIX}: {exc}")]
                )
                continue

            if self.config.dry_run:
                report.dry_run += 1
                report.rows.append(
                    RowResult(row_index, "dry_run", message=json.dumps(payload, ensure_ascii=False))
                )
                continue

            try:
                created = self.pancake.create_order(payload)
            except (PancakeApiError, ValidationError) as exc:
                report.failed += 1
                report.rows.append(RowResult(row_index, "failed", message=str(exc)))
                self._write_updates(
                    [self._cell_update(columns[COL_SYNC_STATUS], row_index, f"LỖI: {str(exc)[:200]}")]
                )
                continue

            order_id = self._extract_order_id(created)
            if not order_id:
                # Pancake đã nhận đơn nhưng không trả về id nào nhận ra được.
                # Ghi ô trống = lần chạy sau coi như chưa xử lý = tạo đơn TRÙNG.
                # Nên vẫn phải đánh dấu dòng, kèm custom_id để tra lại trong Pancake.
                order_id = f"?{payload.get('custom_id', 'DA-TAO-KHONG-RO-ID')}"
                self.logger.error(
                    "Dòng %s: Pancake nhận đơn nhưng không trả order id. "
                    "Đánh dấu %r để không tạo trùng; anh tra trong Pancake theo custom_id rồi sửa lại ô này.",
                    row_index,
                    order_id,
                )
            report.created += 1
            report.rows.append(RowResult(row_index, "created", order_id=order_id))

            # Ghi ngược NGAY sau từng đơn, không gom cuối vòng lặp.
            # Nếu tiến trình chết giữa chừng mà chưa ghi, lần chạy sau sẽ thấy
            # ô trống và tạo đơn TRÙNG - khách nhận hai kiện COD.
            try:
                self._write_updates(
                    [
                        self._cell_update(columns[COL_ORDER_ID], row_index, order_id),
                        self._cell_update(columns[COL_SYNC_STATUS], row_index, "OK"),
                    ]
                )
            except Exception:  # noqa: BLE001
                self.logger.critical(
                    "ĐÃ TẠO đơn Pancake %s cho dòng %s NHƯNG ghi ngược Sheet thất bại. "
                    "Điền tay order id vào cột %r trước lần chạy sau, nếu không sẽ tạo đơn trùng. "
                    "Đơn mang custom_id=%s.",
                    order_id,
                    row_index,
                    COL_ORDER_ID,
                    payload.get("custom_id", ""),
                )
                raise

        return report

    # ─────────────────────────── payload ────────────────────────────

    @property
    def sku_map(self) -> dict[str, dict[str, Any]]:
        if self._sku_map is None:
            self._sku_map = self._load_sku_map()
        return self._sku_map

    @property
    def _uses_pancake_catalog(self) -> bool:
        return self.config.sku_map_path.strip().lower().startswith("pancake:")

    def resolve_jennie_items(self, row: list[Any], header: list[str]) -> list[dict[str, Any]]:
        """Map mã sản phẩm + màu + size Jennie sang biến thể Pancake."""
        if not self._pancake_catalog:
            _ = self.sku_map
        get = lambda *names: self._cell(row, header, *names)  # noqa: E731
        raw_codes = get("SKU Code", "sku_code", "sku_codes")
        codes = self._split_order_values(raw_codes)
        if not codes:
            codes = self._split_order_values(
                get("Mã SP", "Mã sản phẩm", "ma_sp", "SKU", "sku")
            )
        if not codes:
            raise ValidationError("thiếu SKU sản phẩm Jennie trong lead")

        items: list[dict[str, Any]] = []
        for index, raw_code in enumerate(codes, start=1):
            code = self._normalize_jennie_code(raw_code)
            color = str(
                get(f"mau{index}", f"color{index}", f"Màu {index}")
                or (get("Màu", "mau", "color") if len(codes) == 1 else "")
            ).strip()
            size = str(
                get(f"size{index}", f"Size {index}")
                or (get("Size", "size") if len(codes) == 1 else "")
            ).strip()
            quantity_raw = get(f"sl{index}", f"qty{index}", f"Quantity {index}")
            if not quantity_raw and len(codes) == 1:
                quantity_raw = get("Số lượng", "sl", "quantity", "qty")
            quantity = self._to_int(quantity_raw) or 1

            chosen = self._choose_jennie_variation(
                self._pancake_catalog.get(code, []), color=color, size=size
            )
            if not chosen:
                raise ValidationError(
                    f"không map được biến thể Jennie {code} / màu {color or '?'} / size {size or '?'}"
                )
            retail = self._to_int(chosen.get("retail_price"))
            item: dict[str, Any] = {
                "variation_id": chosen["variation_id"],
                "quantity": quantity,
                "variation_info": {"sku": chosen.get("variation_sku") or code},
            }
            if retail > 0:
                item["variation_info"]["retail_price"] = retail
            items.append(item)
        return items

    def resolve_items(self, selected_skus: str, fallback_sku: str = "") -> list[dict[str, Any]]:
        """Tách chuỗi Selected SKUs thành danh sách item Pancake, gộp số lượng.

        Mỗi item mang retail_price = giá niêm yết gốc trong Pancake. Phần khuyến
        mãi Bundle KHÔNG gắn vào từng dòng hàng mà dồn vào total_discount ở cấp
        đơn (xem _order_discount_minor) — gắn cả hai chỗ thì Pancake có thể trừ
        hai lần và thu thiếu tiền.
        """
        found = SKU_PATTERN.findall(str(selected_skus or ""))
        if not found and fallback_sku:
            candidate = " ".join(str(fallback_sku).upper().split())
            if candidate in self.sku_map:
                found = [candidate]

        counts: dict[str, int] = {}
        for raw in found:
            key = " ".join(str(raw).upper().split())
            if key in self.sku_map:
                counts[key] = counts.get(key, 0) + 1
            else:
                self.logger.warning("SKU không có trong map, bỏ qua: %s", key)

        items: list[dict[str, Any]] = []
        for sku, qty in counts.items():
            entry = self.sku_map[sku]
            retail = self._to_int(entry.get("retail_price_minor"))
            item: dict[str, Any] = {
                "variation_id": entry["variation_id"],
                "quantity": qty,
                "variation_info": {"sku": sku},
            }
            if retail > 0:
                item["variation_info"]["retail_price"] = retail
            items.append(item)
        return items

    def _order_discount_minor(self, items: list[dict[str, Any]], total_minor: int) -> int:
        """Khuyến mãi Bundle tính ở cấp đơn: tổng giá niêm yết trừ đi số khách trả.

        Không đọc chữ "Bundle 2 ลด 10%" để suy ra 10% — lấy thẳng hiệu số giữa
        giá gốc và cột Order value mà landing đã tính sẵn. Cách này ăn theo đúng
        phép làm tròn của landing và tự đúng cả khi anh đổi mức giảm sau này,
        không phải sửa code.
        """
        subtotal = sum(
            self._to_int(i.get("variation_info", {}).get("retail_price")) * self._to_int(i.get("quantity"))
            for i in items
        )
        if subtotal <= 0 or total_minor <= 0:
            return 0
        discount = subtotal - total_minor
        if discount <= 0:
            # Khách trả bằng hoặc cao hơn giá niêm yết -> không giảm gì.
            # Tuyệt đối không trả số âm, Pancake sẽ hiểu thành cộng thêm tiền.
            return 0
        if discount >= subtotal:
            # Giảm bằng cả đơn = biếu không. Gần như chắc chắn dữ liệu sai.
            self.logger.error(
                "Bỏ qua giảm giá bất thường: giá gốc %s, khách trả %s", subtotal, total_minor
            )
            return 0
        return discount

    def build_order_payload(self, row: list[Any], header: list[str]) -> dict[str, Any]:
        get = lambda *names: self._cell(row, header, *names)  # noqa: E731

        scale = self.config.price_scale if self.config.price_scale > 0 else 1
        if self._uses_pancake_catalog:
            items = self.resolve_jennie_items(row, header)
        else:
            items = self.resolve_items(
                get("Selected SKUs", "selected_skus"), get("SKU Code", "sku_code")
            )
        if not items:
            raise ValidationError(
                f"không map được SKU nào từ {get('Selected SKUs', 'selected_skus')!r}"
            )

        total_minor = self._to_minor(
            get("Order value", "Giá trị đơn", "Tổng đơn", "tong_don", "value"), scale
        )
        name = str(
            get(
                "ชื่อผู้รับ / Recipient",
                "Tên người nhận",
                "Tên khách",
                "nguoi_nhan",
                "ten_nguoi_nhan",
                "c_ten",
            )
        ).strip()
        phone = self._clean_phone(
            get("เบอร์โทรศัพท์ / Phone", "Số điện thoại", "SĐT", "Điện thoại", "sdt", "c_sdt")
        )
        if not phone:
            raise ValidationError("thiếu số điện thoại")

        note = " | ".join(
            str(part)
            for part in (
                get("ชื่อสินค้า / Product Name", "Tên sản phẩm", "san_pham", "ten_san_pham"),
                get("Combo", "combo"),
                get("Item choices in combo", "SP trong combo", "item_choices"),
                get("Note", "Ghi chú", "ghi_chu", "c_ghichu"),
                f"Nguồn: {get('Data source', 'Nguồn dữ liệu', 'nguon_du_lieu') or 'Dropo landing'}",
            )
            if part
        )

        address = str(get("ที่อยู่เต็ม / Address + ZIP", "Địa chỉ", "Địa chỉ giao hàng", "dia_chi_zip", "c_diachi")).strip()
        province = str(get("จังหวัด / Province", "Tỉnh/Thành", "Tỉnh thành", "tinh", "c_tinh")).strip()
        if province and province not in address:
            address = ", ".join(part for part in (address, province) if part)
        post_code = str(get("รหัสไปรษณีย์ / ZIP", "ZIP", "Mã ZIP", "zip", "c_zip")).strip()
        shipping_address = self._enrich_thai_shipping_address(
            {
                "full_name": name,
                "phone_number": phone,
                "address": address,
                "post_code": post_code,
                "country_code": "66",
            },
            raw_address=address,
            province=province,
            post_code=post_code,
        )
        source_order_id = str(get("Order ID", "ma_don", "order_id")).strip()
        # Một số tab Dropo cũ giữ header `ma_don` nhưng dữ liệu thực tế ở ô đó
        # lại là tóm tắt sản phẩm. Chỉ dùng mã đơn có hình dạng JC/DROPO làm
        # custom_id; nếu không thì quay về timestamp + 4 số điện thoại.
        if source_order_id and not re.fullmatch(r"(?:JC|DROPO)[A-Z0-9_-]{4,}", source_order_id, re.IGNORECASE):
            source_order_id = ""
        custom_id = (
            source_order_id
            if source_order_id
            else self._build_custom_id(get("Thời gian", "created_at", "time"), phone)
        )

        payload: dict[str, Any] = {
            # Khoá định danh suy ra từ chính dữ liệu lead (thời gian + đuôi SĐT),
            # ổn định qua mọi lần chạy. Nếu có đơn trùng lọt vào Pancake thì vẫn
            # tra ra được bằng custom_id thay vì phải dò tay.
            "custom_id": custom_id,
            "items": items,
            "shipping_address": shipping_address,
            "bill_full_name": name,
            "bill_phone_number": phone,
            "note": note,
            "status": self.config.order_status,
            "is_free_shipping": True,
            "shipping_fee": 0,
            # Pancake dùng đơn vị nhỏ (849 THB -> 84900). Gửi số thô 849 thì đơn
            # sẽ mang giá trị 8,49 THB.
            "total_price": total_minor,
            "total_discount": self._order_discount_minor(items, total_minor),
            "currency": str(get("Currency", "Tiền tệ", "currency_code") or "THB").strip(),
        }
        if self.config.warehouse_id:
            payload["warehouse_id"] = self.config.warehouse_id
        return payload

    def _enrich_thai_shipping_address(
        self,
        shipping_address: dict[str, Any],
        *,
        raw_address: str,
        province: str,
        post_code: str,
    ) -> dict[str, Any]:
        """Bổ sung ID địa chỉ Pancake để đơn không bị báo thiếu địa chỉ.

        Landing chỉ thu địa chỉ tự do + tỉnh + ZIP. Pancake vẫn nhận được chuỗi
        tự do nhưng không coi đó là địa chỉ giao vận hợp lệ nếu thiếu
        province_id/district_id/commune_id. Geo API được gọi ở đây để map các
        tên tỉnh/quận/phường có trong địa chỉ về đúng ID của Pancake.

        Fake client trong unit test không có các geo methods nên vẫn giữ payload
        cũ; client thật luôn có các methods này.
        """
        if not all(
            hasattr(self.pancake, method)
            for method in ("list_geo_provinces", "list_geo_districts", "list_geo_communes")
        ):
            return shipping_address

        postcode = re.sub(r"\D", "", str(post_code or ""))
        address_key = self._norm_geo_match(raw_address)
        province_rows = self._geo_rows("provinces", "", "")
        province_row = self._choose_geo_row(province_rows, province, address_key)
        if not province_row:
            raise ValidationError(
                "không chuẩn hóa được tỉnh/thành địa chỉ Thái theo Pancake; "
                "kiểm tra lại tỉnh và mã ZIP"
            )

        province_id = str(province_row.get("id") or "").strip()
        province_name = str(province_row.get("name") or province).strip()
        shipping_address.update(
            {
                "province_id": province_id,
                "province_name": province_name,
                "render_type": "old",
            }
        )

        district_rows = self._geo_rows("districts", province_id, "")
        district_row = self._choose_geo_row(district_rows, "", address_key, postcode=postcode)
        if not district_row:
            raise ValidationError(
                "không chuẩn hóa được quận/huyện địa chỉ Thái từ địa chỉ tự do; "
                "bổ sung tên quận/huyện trong ô địa chỉ"
            )

        district_id = str(district_row.get("id") or "").strip()
        district_name = str(district_row.get("name") or "").strip()
        shipping_address.update(
            {
                "district_id": district_id,
                "district_name": district_name,
            }
        )

        commune_rows = self._geo_rows("communes", province_id, district_id)
        commune_row = self._choose_geo_row(commune_rows, "", address_key, postcode=postcode)
        if not commune_row:
            raise ValidationError(
                "không chuẩn hóa được phường/xã địa chỉ Thái từ địa chỉ tự do; "
                "bổ sung tên phường/xã trong ô địa chỉ"
            )

        commune_id = str(commune_row.get("id") or "").strip()
        commune_name = str(commune_row.get("name") or "").strip()
        shipping_address.update(
            {
                "commune_id": commune_id,
                "commnue_name": commune_name,
                "full_address": ", ".join(
                    part
                    for part in (raw_address, commune_name, district_name, province_name, postcode)
                    if part
                ),
            }
        )
        self.logger.info(
            "Chuẩn hóa địa chỉ Pancake: province=%s district=%s commune=%s postcode=%s",
            province_id,
            district_id,
            commune_id,
            postcode,
        )
        return shipping_address

    def _geo_rows(self, kind: str, province_id: str, district_id: str) -> list[dict[str, Any]]:
        key = (kind, f"{province_id}:{district_id}")
        if key not in self._geo_cache:
            if kind == "provinces":
                rows = self.pancake.list_geo_provinces(country_code="66")
            elif kind == "districts":
                rows = self.pancake.list_geo_districts(province_id, country_code="66")
            else:
                rows = self.pancake.list_geo_communes(province_id, district_id, country_code="66")
            self._geo_cache[key] = rows
        return self._geo_cache[key]

    @classmethod
    def _choose_geo_row(
        cls,
        rows: list[dict[str, Any]],
        preferred_name: str,
        address_key: str,
        *,
        postcode: str = "",
    ) -> dict[str, Any] | None:
        if not rows:
            return None

        def names(row: dict[str, Any]) -> list[str]:
            raw = [row.get("name"), row.get("name_en")]
            result: list[str] = []
            for value in raw:
                value_key = cls._norm_geo_match(value)
                if value_key:
                    result.append(value_key)
            return result

        preferred_key = cls._norm_geo_match(preferred_name)
        if preferred_key:
            exact = [row for row in rows if preferred_key in names(row)]
            if exact:
                return exact[0]
            contains = [row for row in rows if any(preferred_key in value for value in names(row))]
            if len(contains) == 1:
                return contains[0]

        postcode_rows = [
            row
            for row in rows
            if postcode and postcode in {str(code).strip() for code in (row.get("postcode") or [])}
        ]
        address_hits = [
            row
            for row in (postcode_rows or rows)
            if any(value and value in address_key for value in names(row) if len(value) >= 3)
        ]
        if len(address_hits) == 1:
            return address_hits[0]
        if len(postcode_rows) == 1:
            return postcode_rows[0]
        return None

    # ──────────────────────────── sheet ─────────────────────────────

    def _fetch_sheet_values(self) -> list[list[Any]]:
        """Đọc TOÀN BỘ dòng có dữ liệu của tab.

        Dải phải mở (A:BZ), không được chốt số dòng. Dropo ghi lead mới xuống
        cuối sheet, nên một dải kiểu A1:AZ500 sẽ khiến mọi lead sau dòng 500
        rơi ra ngoài vùng đọc và KHÔNG BAO GIỜ được đồng bộ — im lặng, không
        báo lỗi, đơn chỉ đơn giản là ngừng chảy sang Pancake.
        """
        rng = f"{self._escape_tab(self.config.sheet_tab)}!A:BZ"
        url = f"{SHEETS_API_BASE}/{self.config.spreadsheet_id}/values/{rng}"
        payload = self._sheets_request("GET", url)
        values = payload.get("values")
        if not isinstance(values, list):
            return []
        if self.config.max_rows > 0 and len(values) > self.config.max_rows + 1:
            # Cảnh báo chứ không cắt: cắt là quay lại đúng cái bug ở trên.
            self.logger.warning(
                "Sheet có %s dòng, vượt DROPO_PANCAKE_MAX_ROWS=%s. Vẫn quét hết; "
                "cân nhắc tách bớt dòng cũ sang tab lưu trữ cho nhẹ.",
                len(values) - 1,
                self.config.max_rows,
            )
        return values

    def _normalize_sheet_timestamps(self, rows: list[list[Any]], header: list[str]) -> None:
        """Đổi timestamp ISO UTC do Dropo ghi thành giờ Hà Nội (+07:00).

        Dropo hiện ghi chuỗi ISO có hậu tố ``+00:00`` vào Sheet. Vì đó là
        text, timezone của Google Sheet (Asia/Saigon) không tự chuyển giờ.
        Chỉ chuẩn hóa các ô có timezone/ISO rõ ràng; các timestamp đã là text
        địa phương hoặc giá trị không hợp lệ được giữ nguyên.
        """
        time_index = next(
            (header.index(name) for name in ("Thời gian", "created_at", "time") if name in header),
            None,
        )
        if time_index is None:
            return

        updates: list[dict[str, Any]] = []
        changed = 0
        for row_index, row in enumerate(rows, start=2):
            if time_index >= len(row):
                continue
            raw = str(row[time_index] or "").strip()
            if not raw or ("T" not in raw and "Z" not in raw and "+" not in raw[10:]):
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            normalized = parsed.astimezone(HANOI_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if raw == normalized:
                continue
            updates.append(self._cell_update(time_index, row_index, normalized))
            changed += 1

        if updates:
            self._write_updates(updates)
            self.logger.info("Đã chuẩn hóa %s timestamp Dropo sang giờ Hà Nội (+07:00).", changed)

    def _ensure_tracking_columns(self, header: list[str]) -> dict[str, int]:
        """Trả về map tên cột -> index, tự thêm cột theo dõi nếu Sheet chưa có."""
        columns: dict[str, int] = {}
        appended: list[tuple[int, str]] = []
        for label in (COL_ORDER_ID, COL_SYNC_STATUS):
            if label in header:
                columns[label] = header.index(label)
                continue
            index = len(header)
            header.append(label)
            columns[label] = index
            appended.append((index, label))

        for index, label in appended:
            self._write_updates([self._cell_update(index, 1, label)])
            self.logger.info("Đã thêm cột %r vào Sheet tại cột %s", label, self._a1_col(index))
        return columns

    def _write_updates(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        url = f"{SHEETS_API_BASE}/{self.config.spreadsheet_id}/values:batchUpdate"
        body = {"valueInputOption": "RAW", "data": updates}
        self._sheets_request("POST", url, data=body)

    def _cell_update(self, col_index: int, row_index: int, value: Any) -> dict[str, Any]:
        cell = f"{self._escape_tab(self.config.sheet_tab)}!{self._a1_col(col_index)}{row_index}"
        return {"range": cell, "values": [[value]]}

    def _sheets_request(
        self, method: str, url: str, *, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._google_access_token()}",
            "Accept": "application/json",
        }
        response = self.session.request(
            method=method.upper(), url=url, headers=headers, json=data, timeout=30
        )
        if response.status_code == 401:
            # token hết hạn giữa chừng -> refresh 1 lần rồi thử lại
            self._access_token = ""
            headers["Authorization"] = f"Bearer {self._google_access_token()}"
            response = self.session.request(
                method=method.upper(), url=url, headers=headers, json=data, timeout=30
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Google Sheets API lỗi ({response.status_code}): {str(response.text)[:300]}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Google Sheets API trả JSON không hợp lệ: {str(response.text)[:200]}") from exc
        return payload if isinstance(payload, dict) else {}

    def _google_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        response = self.session.request(
            method="POST",
            url=self.config.oauth_token_uri or DEFAULT_TOKEN_URI,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "client_id": self.config.oauth_client_id,
                "client_secret": self.config.oauth_client_secret,
                "refresh_token": self.config.oauth_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OAuth Google lỗi ({response.status_code}): {str(response.text)[:200]}")
        payload = response.json()
        token = str((payload or {}).get("access_token", "")).strip()
        if not token:
            raise RuntimeError("Không lấy được access_token từ Google.")
        self._access_token = token
        return token

    # ──────────────────────────── helpers ───────────────────────────

    def _load_sku_map(self) -> dict[str, dict[str, Any]]:
        raw_path = self.config.sku_map_path
        if not raw_path:
            return {}
        if raw_path.strip().lower().startswith("pancake:"):
            return self._load_pancake_sku_map()
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / raw_path
        if not path.exists():
            self.logger.error("Không thấy SKU map tại %s", path)
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.error("SKU map hỏng (%s): %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            " ".join(str(k).upper().split()): v
            for k, v in data.items()
            if isinstance(v, dict) and v.get("variation_id")
        }

    def _load_pancake_sku_map(self) -> dict[str, dict[str, Any]]:
        """Đọc catalog thật của shop Pancake và lập map theo mã sản phẩm."""
        try:
            products = self.pancake.list_products(page_size=500)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Không đọc được catalog Pancake Jennie: %s", exc)
            return {}

        result: dict[str, dict[str, Any]] = {}
        catalog: dict[str, list[dict[str, Any]]] = {}
        for product in products:
            code = self._normalize_jennie_code(product.get("custom_id", ""))
            if not code:
                continue
            for variation in product.get("variations", []) or []:
                if not isinstance(variation, dict):
                    continue
                variation_id = str(variation.get("id", "")).strip()
                if not variation_id or variation.get("is_removed"):
                    continue
                fields = variation.get("fields") or []
                field_values: list[str] = []
                size = ""
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    value = str(field.get("value", "")).strip()
                    key_value = str(field.get("keyValue", "")).strip()
                    field_values.extend([value, key_value])
                    if str(field.get("name", "")).strip().lower() in {"size", "ไซซ์"}:
                        size = value or key_value
                entry = {
                    "variation_id": variation_id,
                    "variation_sku": str(variation.get("custom_id", "")).strip(),
                    "retail_price": variation.get("retail_price")
                    or variation.get("retail_price_currency_original")
                    or variation.get("retail_price_after_discount")
                    or 0,
                    "size": size,
                    "field_values": field_values,
                }
                catalog.setdefault(code, []).append(entry)
                result[entry["variation_sku"] or variation_id] = entry
        self._pancake_catalog = catalog
        self.logger.info(
            "Đã đọc %s sản phẩm và %s biến thể từ catalog Pancake.",
            len(catalog),
            len(result),
        )
        return result

    @staticmethod
    def _split_order_values(value: Any) -> list[str]:
        return [part.strip() for part in re.split(r"[,;|]", str(value or "")) if part.strip()]

    @staticmethod
    def _normalize_jennie_code(value: Any) -> str:
        raw = "".join(str(value or "").upper().split()).replace("_", "-")
        if re.fullmatch(r"JC-[A-Z]+-\d+", raw):
            return raw
        match = re.fullmatch(r"JC(CV|[VAQ])[- ]?(\d+)", raw)
        if match:
            return f"JC-{match.group(1)}-{match.group(2)}"
        return raw

    @staticmethod
    def _norm_match(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return re.sub(r"[^A-Z0-9]", "", text.upper())

    @staticmethod
    def _norm_geo_match(value: Any) -> str:
        """Chuẩn hóa tên địa danh nhưng giữ chữ Thái để còn match được."""
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(ch for ch in text if ch.isalnum())

    @classmethod
    def _choose_jennie_variation(
        cls,
        candidates: list[dict[str, Any]],
        *,
        color: str,
        size: str,
    ) -> dict[str, Any] | None:
        size_key = cls._norm_match(size)
        color_key = cls._norm_match(color)
        sized = [c for c in candidates if not size_key or cls._norm_match(c.get("size")) == size_key]
        if not sized:
            return None
        if not color_key:
            return sized[0]
        scored: list[tuple[int, dict[str, Any]]] = []
        for candidate in sized:
            haystack = cls._norm_match(" ".join(candidate.get("field_values", [])))
            if not haystack:
                continue
            if color_key == haystack:
                score = 3
            elif color_key in haystack:
                score = 2
            elif haystack in color_key:
                score = 1
            else:
                continue
            scored.append((score, candidate))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1]

    @staticmethod
    def _pad(row: list[Any], width: int) -> list[Any]:
        padded = list(row)
        while len(padded) < width:
            padded.append("")
        return padded

    @staticmethod
    def _cell(row: list[Any], header: list[str], *names: str) -> Any:
        for name in names:
            if name in header:
                index = header.index(name)
                if index < len(row):
                    return row[index]
        return ""

    @staticmethod
    def _build_custom_id(timestamp: Any, phone: str) -> str:
        """Khoá định danh ổn định: DROPO-<chữ số của thời gian>-<4 số cuối SĐT>."""
        stamp = re.sub(r"[^\d]", "", str(timestamp or ""))[:14]
        tail = re.sub(r"[^\d]", "", str(phone or ""))[-4:]
        if not stamp:
            return f"DROPO-{tail}" if tail else "DROPO"
        return f"DROPO-{stamp}-{tail}" if tail else f"DROPO-{stamp}"

    @staticmethod
    def _clean_phone(value: Any) -> str:
        return re.sub(r"[^\d+]", "", str(value or "")).strip()

    @staticmethod
    def _to_int(value: Any) -> int:
        digits = re.sub(r"[^\d]", "", str(value or ""))
        return int(digits) if digits else 0

    @staticmethod
    def _to_minor(value: Any, scale: int) -> int:
        """Đổi số tiền người đọc được (849 / '1,299' / '2338.00') sang đơn vị nhỏ.

        Ô trong Sheet có thể lẫn dấu phân cách nghìn hoặc phần thập phân, nên
        không dùng _to_int (nó gộp hết chữ số lại: '1,299' -> 1299 đúng, nhưng
        '2338.00' -> 233800 sai).
        """
        raw = str(value or "").strip()
        if not raw:
            return 0
        cleaned = re.sub(r"[^\d.,\-]", "", raw)
        if not cleaned:
            return 0
        # Dấu phẩy đứng trước đúng 3 chữ số cuối là phân cách nghìn -> bỏ.
        cleaned = re.sub(r",(?=\d{3}\b)", "", cleaned)
        cleaned = cleaned.replace(",", ".")
        # Còn nhiều dấu chấm thì tất cả trừ cái cuối là phân cách nghìn.
        if cleaned.count(".") > 1:
            head, _, tail = cleaned.rpartition(".")
            cleaned = head.replace(".", "") + "." + tail
        try:
            amount = float(cleaned)
        except ValueError:
            return 0
        if amount <= 0:
            return 0
        return int(round(amount * (scale if scale > 0 else 1)))

    @staticmethod
    def _extract_order_id(created: dict[str, Any]) -> str:
        for key in ("id", "order_id", "custom_id"):
            raw = created.get(key)
            if raw not in (None, ""):
                return str(raw)
        return ""

    @staticmethod
    def _escape_tab(title: str) -> str:
        safe = str(title or "Leads")
        if re.fullmatch(r"[A-Za-z0-9_]+", safe):
            return safe
        return "'" + safe.replace("'", "''") + "'"

    @staticmethod
    def _a1_col(index: int) -> str:
        """0 -> A, 25 -> Z, 26 -> AA."""
        if index < 0:
            raise ValueError("Chỉ số cột không hợp lệ.")
        letters = ""
        current = index
        while True:
            letters = chr(ord("A") + current % 26) + letters
            current = current // 26 - 1
            if current < 0:
                break
        return letters
