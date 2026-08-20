from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from app.dropo_pancake_bridge import BridgeConfig, DropoPancakeBridge
from app.exceptions import PancakeApiError, ValidationError


HEADER = [
    "Thời gian",
    "Nguồn",
    "ชื่อผู้รับ / Recipient",
    "เบอร์โทรศัพท์ / Phone",
    "ที่อยู่เต็ม / Address + ZIP",
    "รหัสไปรษณีย์ / ZIP",
    "รหัสสินค้า / Product Code",
    "SKU Code",
    "ชื่อสินค้า / Product Name",
    "Combo",
    "ตัวเลือกทั้งหมด / Variant + Items",
    "Selected SKUs",
    "Item choices in combo",
    "Quantity",
    "Note",
    "Order value",
    "Currency",
    "Unit price",
    "Pancake shop ID",
    "Data source",
    "Pancake Order ID",
    "Sync status",
]

SKU_MAP = {
    "VXV002-DEN-M": {"variation_id": "var-den-m", "product_code": "VXV002"},
    "VXV002-DEN-L": {"variation_id": "var-den-l", "product_code": "VXV002"},
    "VXV002-XANH LA-M": {"variation_id": "var-xanh-m", "product_code": "VXV002"},
    "VXV009-XANH MINT-M": {"variation_id": "var-mint-m", "product_code": "VXV009"},
    "VXV009-DEN-XL": {"variation_id": "var-den-xl", "product_code": "VXV009"},
}


def make_row(**overrides: Any) -> list[Any]:
    base = {
        "ชื่อผู้รับ / Recipient": "Somchai",
        "เบอร์โทรศัพท์ / Phone": "081-234-5678",
        "ที่อยู่เต็ม / Address + ZIP": "Bangkok test address",
        "รหัสไปรษณีย์ / ZIP": "10110",
        "SKU Code": "VXV002",
        "ชื่อสินค้า / Product Name": "VXV002 Midnight Veil",
        "Combo": "Bundle 2",
        "Selected SKUs": "Item 1: (VXV002-DEN-M) · Item 2: (VXV002-DEN-M)",
        "Order value": "2,338",
        "Currency": "THB",
        "Data source": "Pancake VAYXA",
    }
    base.update(overrides)
    return [base.get(col, "") for col in HEADER]


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Giả lập requests: trả OAuth token, sheet values, và ghi nhận batchUpdate."""

    def __init__(self, values: list[list[Any]]) -> None:
        self.values = values
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []

    def request(self, *, method: str, url: str, headers=None, json=None, data=None, timeout=None):
        self.calls.append((method, url))
        if "oauth2.googleapis.com" in url or url.endswith("/token"):
            return FakeResponse(200, {"access_token": "tok-123"})
        if "values:batchUpdate" in url:
            self.updates.extend((json or {}).get("data", []))
            return FakeResponse(200, {"totalUpdatedCells": len((json or {}).get("data", []))})
        if "/values/" in url:
            return FakeResponse(200, {"values": self.values})
        raise AssertionError(f"URL không mong đợi: {url}")


class FlakySheetsSession(FakeSession):
    def __init__(self, values: list[list[Any]], failures: int) -> None:
        super().__init__(values)
        self.failures = failures

    def request(self, *, method: str, url: str, headers=None, json=None, data=None, timeout=None):
        if "/values/" in url and self.failures > 0:
            self.failures -= 1
            raise requests.ConnectionError("simulated connection reset")
        return super().request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            data=data,
            timeout=timeout,
        )


class FakePancake:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {"id": "PC-1001"}
        self.error = error
        self.payloads: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def create_order(self, payload: dict, **_: Any) -> dict:
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.response


class GeoFakePancake(FakePancake):
    def list_geo_provinces(self, **_: Any) -> list[dict[str, Any]]:
        return [{"id": "66_R92277", "name": "กรุงเทพมหานคร/ Bangkok", "name_en": "กรุงเทพมหานคร"}]

    def list_geo_districts(self, province_id: str, **_: Any) -> list[dict[str, Any]]:
        assert province_id == "66_R92277"
        return [{
            "id": "66_R589",
            "name": "ยานนาวา/ Yan Nawa",
            "name_en": "ยานนาวา",
            "postcode": [10120],
            "province_id": province_id,
        }]

    def list_geo_communes(self, province_id: str, district_id: str, **_: Any) -> list[dict[str, Any]]:
        assert province_id == "66_R92277"
        assert district_id == "66_R589"
        return [{
            "id": "66_R0000014",
            "name": "บางโพงพาง/ Bang Phongphang",
            "name_en": "บางโพงพาง",
            "postcode": [10120],
            "province_id": province_id,
            "district_id": district_id,
        }]


class AmbiguousBangkokGeoFakePancake(GeoFakePancake):
    def list_geo_districts(self, province_id: str, **_: Any) -> list[dict[str, Any]]:
        return [{
            "id": "66_R586",
            "name": "ลาดพร้าว/ Lat Pharo",
            "name_en": "ลาดพร้าว",
            "postcode": [10230],
            "province_id": province_id,
        }]

    def list_geo_communes(self, province_id: str, district_id: str, **_: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "66_R0000094",
                "name": "จรเข้บัว/ Chorakhe Bua",
                "name_en": "จรเข้บัว",
                "postcode": [10230],
                "province_id": province_id,
                "district_id": district_id,
            },
            {
                "id": "66_R0000093",
                "name": "ลาดพร้าว/ Lat Phrao",
                "name_en": "ลาดพร้าว",
                "postcode": [10230],
                "province_id": province_id,
                "district_id": district_id,
            },
        ]

def build_bridge(
    values: list[list[Any]],
    *,
    dry_run: bool = False,
    pancake: FakePancake | None = None,
    sku_map: dict | None = None,
) -> tuple[DropoPancakeBridge, FakeSession, FakePancake]:
    config = BridgeConfig(
        enabled=True,
        dry_run=dry_run,
        spreadsheet_id="sheet-1",
        sheet_tab="Leads",
        oauth_client_id="cid",
        oauth_client_secret="secret",
        oauth_refresh_token="refresh",
    )
    session = FakeSession(values)
    client = pancake or FakePancake()
    bridge = DropoPancakeBridge(
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        config=config,
        pancake_client=client,  # type: ignore[arg-type]
        session=session,
    )
    bridge._sku_map = SKU_MAP if sku_map is None else sku_map
    return bridge, session, client


# ───────────────────────────── resolve_items ─────────────────────────────


def test_gop_so_luong_khi_bundle_trung_sku():
    bridge, _, _ = build_bridge([HEADER])
    items = bridge.resolve_items("Item 1: (VXV002-DEN-M) · Item 2: (VXV002-DEN-M)")
    assert items == [
        {"variation_id": "var-den-m", "quantity": 2, "variation_info": {"sku": "VXV002-DEN-M"}}
    ]


def test_sheets_request_retries_transient_connection_reset(monkeypatch):
    bridge, _, _ = build_bridge([HEADER])
    flaky = FlakySheetsSession([HEADER], failures=2)
    bridge.session = flaky
    monkeypatch.setattr("app.dropo_pancake_bridge.time.sleep", lambda _: None)

    values = bridge._fetch_sheet_values()

    assert values == [HEADER]
    assert flaky.failures == 0


def test_bundle_tron_mau_size_tach_dung_item():
    bridge, _, _ = build_bridge([HEADER])
    raw = "Item 1: (VXV009-XANH MINT-M) · Item 2: (VXV009-DEN-XL) · Item 3: (VXV009-XANH MINT-M)"
    items = {i["variation_info"]["sku"]: i["quantity"] for i in bridge.resolve_items(raw)}
    assert items == {"VXV009-XANH MINT-M": 2, "VXV009-DEN-XL": 1}


def test_cat_dung_hau_to_lo_B1():
    bridge, _, _ = build_bridge([HEADER])
    items = bridge.resolve_items("VXV002-XANH LA-M-B1")
    assert items[0]["variation_info"]["sku"] == "VXV002-XANH LA-M"


def test_sku_la_khong_tao_item_rac():
    bridge, _, _ = build_bridge([HEADER])
    assert bridge.resolve_items("VXV003-DEN-M") == []


def test_fallback_dung_cot_sku_khi_selected_rong():
    bridge, _, _ = build_bridge([HEADER])
    assert bridge.resolve_items("", "VXV002-DEN-L")[0]["variation_id"] == "var-den-l"


def test_jennie_bundle_applies_shared_color_and_size_to_every_product():
    bridge, _, _ = build_bridge([HEADER])
    bridge._pancake_catalog = {
        "JC-A-250": [
            {
                "variation_id": "jca250-kem-s",
                "variation_sku": "JC-A-250-KEM-S",
                "size": "S",
                "field_values": ["Kem", "S"],
                "retail_price": 250000,
            },
            {
                "variation_id": "jca250-kem-l",
                "variation_sku": "JC-A-250-KEM-L",
                "size": "L",
                "field_values": ["Kem", "L"],
                "retail_price": 250000,
            },
        ],
        "JC-A-248": [
            {
                "variation_id": "jca248-kem-s",
                "variation_sku": "JC-A-248-KEM-S",
                "size": "S",
                "field_values": ["Kem", "S"],
                "retail_price": 220000,
            },
            {
                "variation_id": "jca248-kem-l",
                "variation_sku": "JC-A-248-KEM-L",
                "size": "L",
                "field_values": ["Kem", "L"],
                "retail_price": 220000,
            },
        ],
    }
    header = ["Mã sản phẩm", "Màu", "Size", "Số lượng"]
    row = ["JC-A-250, JC-A-248", "Kem", "L", "2"]

    items = bridge.resolve_jennie_items(row, header)

    assert [item["variation_id"] for item in items] == ["jca250-kem-l", "jca248-kem-l"]


def test_jennie_bundle_uses_jcpost_rows_for_per_item_color_and_size():
    bridge, _, _ = build_bridge([HEADER])
    bridge._pancake_catalog = {
        "JC-V-123": [{"variation_id": "jcv123-den-s", "variation_sku": "JC-V-123-DEN COC-S", "size": "S", "field_values": ["Đen cộc", "S"], "retail_price": 300000}],
        "JC-A-158": [{"variation_id": "jca158-nau-s", "variation_sku": "JC-A-158-S-NAU", "size": "S", "field_values": ["Nâu", "S"], "retail_price": 230000}],
        "JC-Q-158": [{"variation_id": "jcq158-nau-s", "variation_sku": "JC-Q-158-NAU-S", "size": "S", "field_values": ["Nâu", "S"], "retail_price": 194000}],
    }
    header = ["SKU Code", "Mã sản phẩm", "Màu", "Size", "Số lượng"]
    row = ["JCV123, JCA158, JCQ158", "JCV123", "Đen cộc", "S", "1"]
    details = [["", "JCA158", "Nâu", "S", "1"], ["", "JCQ158", "Nâu", "S", "1"]]

    items = bridge.resolve_jennie_items(row, header, bundle_rows=details)

    assert [item["variation_id"] for item in items] == ["jcv123-den-s", "jca158-nau-s", "jcq158-nau-s"]


def test_geo_match_tach_ten_thai_va_ten_anh():
    bridge, _, _ = build_bridge([HEADER])
    rows = [{"id": "ranong", "name": "ระนอง/ Ranong", "name_en": "ระนอง"}]

    chosen = bridge._choose_geo_row(rows, "", bridge._norm_geo_match("Ranong Province 85000"))

    assert chosen["id"] == "ranong"


# ───────────────────────────── build_payload ─────────────────────────────


def test_payload_chuan_hoa_sdt_va_tong_tien():
    bridge, _, _ = build_bridge([HEADER])
    payload = bridge.build_order_payload(make_row(), HEADER)
    assert payload["shipping_address"]["phone_number"] == "0812345678"
    # Pancake dùng đơn vị nhỏ: 2338 THB -> 233800.
    assert payload["total_price"] == 233800
    assert payload["currency"] == "THB"
    assert payload["is_free_shipping"] is True
    assert payload["items"][0]["quantity"] == 2


def test_payload_tu_chuan_hoa_dia_chi_thai_thanh_id_pancake():
    bridge, _, _ = build_bridge(
        [HEADER],
        pancake=GeoFakePancake(),
        sku_map=SKU_MAP,
    )
    row = make_row(**{
        "ที่อยู่เต็ม / Address + ZIP": "889/7 บางโพงพาง ยานนาวา กรุงเทพมหานคร 10120",
        "รหัสไปรษณีย์ / ZIP": "10120",
    })
    payload = bridge.build_order_payload(row, HEADER)
    address = payload["shipping_address"]
    assert address["province_id"] == "66_R92277"
    assert address["district_id"] == "66_R589"
    assert address["commune_id"] == "66_R0000014"
    assert address["commnue_name"] == "บางโพงพาง/ Bang Phongphang"


def test_payload_uu_tien_tu_khoa_hanh_chinh_khi_ten_phuong_trung_ten_quan():
    bridge, _, _ = build_bridge(
        [HEADER],
        pancake=AmbiguousBangkokGeoFakePancake(),
        sku_map=SKU_MAP,
    )
    row = make_row(**{
        "ที่อยู่เต็ม / Address + ZIP": "23/36 หมู่บ้านไพรเวท เนอวานา เกษตร-นวมินทร์ ซอยประเสริฐมนูกิจ 29 แยก 6 แขวงจรเข้บัว เขตลาดพร้าว กรุงเทพมหานคร",
        "จังหวัด / Province": "กรุงเทพมหานคร",
        "รหัสไปรษณีย์ / ZIP": "10230",
    })
    payload = bridge.build_order_payload(row, HEADER)
    assert payload["shipping_address"]["district_id"] == "66_R586"
    assert payload["shipping_address"]["commune_id"] == "66_R0000094"


def test_thieu_sdt_thi_bao_loi():
    bridge, _, _ = build_bridge([HEADER])
    with pytest.raises(ValidationError, match="số điện thoại"):
        bridge.build_order_payload(make_row(**{"เบอร์โทรศัพท์ / Phone": ""}), HEADER)


def test_khong_map_duoc_sku_thi_bao_loi():
    bridge, _, _ = build_bridge([HEADER])
    with pytest.raises(ValidationError, match="không map được SKU"):
        bridge.build_order_payload(
            make_row(**{"Selected SKUs": "VXV999-ABC-M", "SKU Code": ""}), HEADER
        )


# ───────────────────────────── run_once ─────────────────────────────


def test_tao_don_va_ghi_nguoc_order_id():
    values = [HEADER, make_row()]
    bridge, session, client = build_bridge(values)
    report = bridge.run_once()

    assert report.created == 1
    assert len(client.payloads) == 1
    ranges = {u["range"]: u["values"][0][0] for u in session.updates}
    assert ranges["Leads!U2"] == "PC-1001"
    assert ranges["Leads!V2"] == "OK"


def test_dong_da_co_order_id_thi_bo_qua_hoan_toan():
    values = [HEADER, make_row(**{"Pancake Order ID": "PC-999"})]
    bridge, _, client = build_bridge(values)
    report = bridge.run_once()

    assert report.created == 0
    assert report.pending == 0
    assert client.payloads == []


def test_chay_lai_khong_tao_don_trung():
    """Lần 1 tạo đơn, lần 2 Sheet đã có order id -> không gọi Pancake nữa."""
    values = [HEADER, make_row()]
    bridge, session, client = build_bridge(values)
    bridge.run_once()
    assert len(client.payloads) == 1

    # mô phỏng Sheet đã được ghi order id
    values[1][HEADER.index("Pancake Order ID")] = "PC-1001"
    bridge2, _, client2 = build_bridge(values, pancake=client)
    bridge2.run_once()
    assert len(client.payloads) == 1  # không tăng


def test_dry_run_khong_goi_pancake():
    values = [HEADER, make_row()]
    bridge, session, client = build_bridge(values, dry_run=True)
    report = bridge.run_once()

    assert report.dry_run == 1
    assert report.created == 0
    assert client.payloads == []
    assert session.updates == []


def test_loi_pancake_ghi_ly_do_va_khong_ghi_order_id():
    values = [HEADER, make_row()]
    bridge, session, _ = build_bridge(
        values, pancake=FakePancake(error=PancakeApiError("Pancake API lỗi (422): thiếu province"))
    )
    report = bridge.run_once()

    assert report.failed == 1
    ranges = {u["range"]: u["values"][0][0] for u in session.updates}
    assert "Leads!U2" not in ranges
    assert ranges["Leads!V2"].startswith("LỖI")


def test_dong_khong_map_duoc_sku_bi_danh_dau_bo_qua():
    values = [HEADER, make_row(**{"Selected SKUs": "VXV999-X-M", "SKU Code": ""})]
    bridge, session, client = build_bridge(values)
    report = bridge.run_once()

    assert report.skipped == 1
    assert client.payloads == []
    ranges = {u["range"]: u["values"][0][0] for u in session.updates}
    assert ranges["Leads!V2"].startswith("BỎ QUA")


def test_dong_da_danh_dau_bo_qua_khong_thu_lai():
    values = [HEADER, make_row(**{"Sync status": "BỎ QUA: không map được SKU"})]
    bridge, _, client = build_bridge(values)
    report = bridge.run_once()

    assert report.pending == 0
    assert client.payloads == []


def test_dong_thieu_sdt_bi_bo_qua_im_lang():
    values = [HEADER, make_row(**{"เบอร์โทรศัพท์ / Phone": ""})]
    bridge, _, client = build_bridge(values)
    report = bridge.run_once()

    assert report.pending == 0
    assert client.payloads == []


def test_tu_them_cot_theo_doi_khi_sheet_chua_co():
    header_short = HEADER[:-2]
    row = make_row()[:-2]
    values = [list(header_short), row]
    bridge, session, _ = build_bridge(values)
    bridge.run_once()

    header_writes = [u for u in session.updates if u["range"].endswith("1")]
    written = {u["values"][0][0] for u in header_writes}
    assert {"Pancake Order ID", "Sync status"} <= written


def test_ton_trong_batch_limit_dem_theo_so_don():
    values = [HEADER] + [make_row() for _ in range(10)]
    bridge, _, client = build_bridge(values)
    bridge.config.batch_limit = 3
    report = bridge.run_once()

    assert report.created == 3
    assert len(client.payloads) == 3


def test_ghi_nguoc_ngay_sau_tung_don_khong_gom_cuoi():
    """Chống tạo đơn trùng: mỗi đơn phải được ghi vào Sheet trước khi tạo đơn kế.

    Nếu gom write-back đến cuối vòng lặp mà tiến trình chết giữa chừng, các đơn
    đã tạo sẽ không có order id trong Sheet -> lần chạy sau tạo lại = khách nhận
    hai kiện COD.
    """
    values = [HEADER] + [make_row() for _ in range(3)]
    bridge, session, client = build_bridge(values)

    order_of_events: list[str] = []
    original_create = client.create_order
    original_write = bridge._write_updates

    def spy_create(payload, **kw):
        order_of_events.append("create")
        return original_create(payload, **kw)

    def spy_write(updates):
        if updates:
            order_of_events.append("write")
        return original_write(updates)

    client.create_order = spy_create  # type: ignore[method-assign]
    bridge._write_updates = spy_write  # type: ignore[method-assign]
    bridge.run_once()

    assert order_of_events == ["create", "write", "create", "write", "create", "write"]


def test_ghi_sheet_that_bai_sau_khi_tao_don_thi_dung_lai():
    """Không được im lặng đi tiếp: đơn đã vào Pancake nhưng Sheet chưa ghi."""
    values = [HEADER] + [make_row() for _ in range(3)]
    bridge, _, client = build_bridge(values)

    calls = {"n": 0}
    original_write = bridge._write_updates

    def flaky_write(updates):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Google Sheets API lỗi (503)")
        return original_write(updates)

    bridge._write_updates = flaky_write  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="503"):
        bridge.run_once()

    # dừng ngay sau đơn đầu, không tạo thêm đơn nào nữa
    assert len(client.payloads) == 1


def test_sheet_rong_khong_no():
    bridge, _, client = build_bridge([])
    report = bridge.run_once()
    assert report.scanned == 0
    assert client.payloads == []


# ───────────────────────────── helpers ─────────────────────────────


@pytest.mark.parametrize(
    "index,expected", [(0, "A"), (20, "U"), (21, "V"), (25, "Z"), (26, "AA"), (51, "AZ")]
)
def test_doi_chi_so_cot_sang_ky_hieu_a1(index, expected):
    assert DropoPancakeBridge._a1_col(index) == expected


def test_escape_ten_tab_co_dau_cach():
    assert DropoPancakeBridge._escape_tab("Leads") == "Leads"
    assert DropoPancakeBridge._escape_tab("Đơn hàng") == "'Đơn hàng'"


def test_is_configured_bao_thieu_gi():
    config = BridgeConfig(spreadsheet_id="", oauth_client_id="", oauth_client_secret="", oauth_refresh_token="")
    bridge = DropoPancakeBridge(
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        config=config,
        pancake_client=FakePancake(),  # type: ignore[arg-type]
        session=FakeSession([]),
    )
    ok, reason = bridge.is_configured()
    assert ok is False
    assert "DROPO_PANCAKE_SHEET_ID" in reason


# ───────────────────────────── custom_id ─────────────────────────────


def test_custom_id_on_dinh_qua_cac_lan_chay():
    bridge, _, _ = build_bridge([HEADER])
    row = make_row(**{"Thời gian": "2026-08-04T03:05:11.506+00:00"})
    a = bridge.build_order_payload(row, HEADER)["custom_id"]
    b = bridge.build_order_payload(row, HEADER)["custom_id"]
    assert a == b == "DROPO-20260804030511-5678"


def test_custom_id_khac_nhau_giua_hai_lead():
    bridge, _, _ = build_bridge([HEADER])
    r1 = make_row(**{"Thời gian": "2026-08-04T03:05:11Z"})
    r2 = make_row(**{"Thời gian": "2026-08-04T09:41:02Z", "เบอร์โทรศัพท์ / Phone": "0899999999"})
    assert (
        bridge.build_order_payload(r1, HEADER)["custom_id"]
        != bridge.build_order_payload(r2, HEADER)["custom_id"]
    )


def test_custom_id_van_co_khi_thieu_thoi_gian():
    bridge, _, _ = build_bridge([HEADER])
    cid = bridge.build_order_payload(make_row(**{"Thời gian": ""}), HEADER)["custom_id"]
    assert cid == "DROPO-5678"


def test_chuyen_timestamp_dropo_utc_sang_gio_hanoi():
    bridge, session, _ = build_bridge([HEADER])
    rows = [make_row(**{"Thời gian": "2026-08-17T06:21:07.409+00:00"})]

    bridge._normalize_sheet_timestamps(rows, HEADER)

    ranges = {u["range"]: u["values"][0][0] for u in session.updates}
    assert ranges["Leads!A2"] == "2026-08-17 13:21:07"


# ───────────────────────────── config từ env ─────────────────────────────


def test_config_uu_tien_bien_theo_profile(monkeypatch):
    monkeypatch.setenv("DROPO_PANCAKE_SHEET_ID", "sheet-chung")
    monkeypatch.setenv("ADS2_DROPO_PANCAKE_SHEET_ID", "sheet-ads2")
    monkeypatch.setenv("ADS2_DROPO_PANCAKE_BRIDGE_ENABLED", "1")
    cfg = BridgeConfig.from_env("ADS2")
    assert cfg.spreadsheet_id == "sheet-ads2"
    assert cfg.enabled is True


def test_config_roi_ve_bien_khong_tien_to(monkeypatch):
    monkeypatch.delenv("ADS2_DROPO_PANCAKE_SHEET_ID", raising=False)
    monkeypatch.setenv("DROPO_PANCAKE_SHEET_ID", "sheet-chung")
    assert BridgeConfig.from_env("ADS2").spreadsheet_id == "sheet-chung"


def test_config_mac_dinh_la_dry_run(monkeypatch):
    monkeypatch.delenv("DROPO_PANCAKE_BRIDGE_DRY_RUN", raising=False)
    monkeypatch.delenv("ADS2_DROPO_PANCAKE_BRIDGE_DRY_RUN", raising=False)
    assert BridgeConfig.from_env("ADS2").dry_run is True, "mặc định phải an toàn"


def test_config_dung_chung_oauth_cua_reconcile_neu_chua_khai_rieng(monkeypatch):
    for key in ("DROPO_PANCAKE_OAUTH_CLIENT_ID", "ADS2_DROPO_PANCAKE_OAUTH_CLIENT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RECONCILE_COD_SHEET_OAUTH_CLIENT_ID", "cid-chung")
    assert BridgeConfig.from_env("ADS2").oauth_client_id == "cid-chung"


# ─────────────────────── đọc hết sheet, không chốt dòng ───────────────────────


def test_doc_dai_mo_khong_chot_so_dong():
    """Dải phải là A:BZ. Chốt A1:AZ500 sẽ làm lead sau dòng 500 biến mất im lặng."""
    bridge, session, _ = build_bridge([HEADER, make_row()])
    bridge.run_once()
    reads = [url for method, url in session.calls if "/values/" in url and "batchUpdate" not in url]
    assert reads, "phải có ít nhất 1 lần đọc sheet"
    assert "A%3ABZ" in reads[0] or "A:BZ" in reads[0], f"dải đọc bị chốt số dòng: {reads[0]}"


def test_van_xu_ly_lead_o_dong_rat_xa():
    """Lead thứ 600 vẫn phải được tạo đơn, không rơi ra ngoài vùng đọc."""
    done = make_row(**{"Pancake Order ID": "PC-cu"})
    values = [HEADER] + [done for _ in range(600)] + [make_row()]
    bridge, _, client = build_bridge(values)
    report = bridge.run_once()

    assert report.created == 1, "lead ở cuối sheet dài phải được xử lý"
    assert len(client.payloads) == 1


def test_sheet_dai_chi_canh_bao_khong_cat_bot(caplog):
    done = make_row(**{"Pancake Order ID": "PC-cu"})
    values = [HEADER] + [done for _ in range(30)] + [make_row()]
    bridge, _, client = build_bridge(values)
    bridge.config.max_rows = 10
    with caplog.at_level("WARNING"):
        report = bridge.run_once()

    assert report.scanned == 31, "không được cắt bớt dòng"
    assert report.created == 1
    assert any("vượt" in r.message or "vượt" in str(r.msg) for r in caplog.records)


def test_pancake_khong_tra_order_id_van_danh_dau_dong():
    """Ô order id không bao giờ được để trống sau khi đơn đã vào Pancake.

    Ô trống = lần chạy sau coi như chưa xử lý = tạo đơn trùng.
    """
    values = [HEADER, make_row(**{"Thời gian": "2026-08-04T03:05:11Z"})]
    bridge, session, _ = build_bridge(values, pancake=FakePancake(response={"success": True}))
    report = bridge.run_once()

    assert report.created == 1
    ranges = {u["range"]: u["values"][0][0] for u in session.updates}
    assert ranges["Leads!U2"], "cột order id phải có dấu, không được rỗng"
    assert "DROPO-20260804030511-5678" in ranges["Leads!U2"]


def test_chay_lai_sau_khi_danh_dau_khong_ro_id_van_khong_tao_trung():
    values = [HEADER, make_row(**{"Thời gian": "2026-08-04T03:05:11Z"})]
    bridge, session, client = build_bridge(values, pancake=FakePancake(response={"success": True}))
    bridge.run_once()
    marked = {u["range"]: u["values"][0][0] for u in session.updates}["Leads!U2"]

    values[1][HEADER.index("Pancake Order ID")] = marked
    bridge2, _, _ = build_bridge(values, pancake=client)
    assert bridge2.run_once().created == 0
    assert len(client.payloads) == 1


# ─────────────────────── giá & khuyến mãi Bundle ────────────────────────
#
# Pancake lưu giá ở đơn vị nhỏ (849 THB -> 84900). Landing bán Bundle 2/3/4
# giảm 10/15/20%, nên số khách trả THẤP HƠN tổng giá niêm yết. Phần chênh này
# đi vào total_discount ở cấp đơn — nếu thiếu, shipper thu COD cao hơn số khách
# thấy lúc đặt và khách từ chối nhận hàng.
#
# Bất biến quan trọng: total_price + total_discount == tổng giá niêm yết.

PRICED_MAP = {
    "VXV002-DEN-M": {
        "variation_id": "var-den-m",
        "product_code": "VXV002",
        "retail_price_minor": 129900,
    },
    "VXV002-DEN-L": {
        "variation_id": "var-den-l",
        "product_code": "VXV002",
        "retail_price_minor": 129900,
    },
}


def priced_payload(**overrides: Any) -> dict:
    bridge, _, _ = build_bridge([HEADER], sku_map=PRICED_MAP)
    return bridge.build_order_payload(make_row(**overrides), HEADER)


def gia_niem_yet(payload: dict) -> int:
    return sum(
        i["variation_info"]["retail_price"] * i["quantity"] for i in payload["items"]
    )


def test_bundle2_giam_dung_10_phan_tram():
    payload = priced_payload(**{"Order value": "2338"})
    assert gia_niem_yet(payload) == 259800
    assert payload["total_discount"] == 26000
    assert payload["total_price"] == 233800
    assert payload["total_price"] + payload["total_discount"] == 259800


def test_khuyen_mai_khong_gan_vao_tung_dong_hang():
    """Gắn cả total_discount lẫn discount_each_product thì Pancake có thể trừ
    hai lần -> thu thiếu tiền. Chỉ được giảm ở MỘT cấp."""
    payload = priced_payload(**{"Order value": "2338"})
    assert "discount_each_product" not in payload["items"][0]
    assert payload["items"][0]["variation_info"]["retail_price"] == 129900


def test_mua_le_khong_giam_gia():
    payload = priced_payload(
        **{"Selected SKUs": "VXV002-DEN-M-B1", "Order value": "1299"}
    )
    assert payload["total_discount"] == 0
    assert payload["total_price"] == 129900


def test_khong_bao_gio_tra_discount_am():
    """Order value cao hơn giá niêm yết (nhập tay sai / có phụ phí) không được
    biến thành số âm — Pancake sẽ hiểu thành CỘNG thêm tiền vào đơn."""
    payload = priced_payload(**{"Order value": "4000"})
    assert payload["total_discount"] == 0


def test_tu_choi_giam_bang_ca_don():
    """Order value = 0 hoặc rác thì không được giảm hết thành hàng biếu."""
    for gia_tri in ("0", "", "abc"):
        payload = priced_payload(**{"Order value": gia_tri})
        assert payload["total_discount"] == 0, gia_tri


def test_sku_chua_co_gia_thi_khong_giam():
    """Map cũ chưa có retail_price_minor vẫn chạy, chỉ là không tính được giảm."""
    bridge, _, _ = build_bridge([HEADER])
    payload = bridge.build_order_payload(make_row(), HEADER)
    assert "retail_price" not in payload["items"][0]["variation_info"]
    assert payload["total_discount"] == 0


def test_bundle_tron_mau_van_cong_dung_gia_goc():
    payload = priced_payload(
        **{
            "Selected SKUs": "Item 1: (VXV002-DEN-M) · Item 2: (VXV002-DEN-L)",
            "Order value": "2338",
        }
    )
    assert len(payload["items"]) == 2
    assert gia_niem_yet(payload) == 259800
    assert payload["total_discount"] == 26000


@pytest.mark.parametrize(
    "raw,mong_doi",
    [
        ("849", 84900),
        ("2,338", 233800),
        ("3676.80", 367680),
        ("1.234,50", 123450),
        ("฿1,299", 129900),
        ("", 0),
        ("0", 0),
        ("-500", 0),
        ("abc", 0),
    ],
)
def test_to_minor_doc_dung_moi_dinh_dang(raw: str, mong_doi: int):
    assert DropoPancakeBridge._to_minor(raw, 100) == mong_doi


def test_price_scale_1_giu_nguyen_so():
    """Shop cấu hình giá theo đơn vị nguyên thì đặt PRICE_SCALE=1, không sửa code."""
    bridge, _, _ = build_bridge([HEADER], sku_map=PRICED_MAP)
    bridge.config.price_scale = 1
    payload = bridge.build_order_payload(make_row(**{"Order value": "2338"}), HEADER)
    assert payload["total_price"] == 2338


def test_country_code_dung_ma_dien_thoai_khong_phai_iso():
    """Don that cua shop tra ve country_code "66" (ma dien thoai Thai Lan).
    Gui "TH" la sai dinh dang -> Pancake khong resolve duoc dia chi."""
    bridge, _, _ = build_bridge([HEADER])
    payload = bridge.build_order_payload(make_row(), HEADER)
    assert payload["shipping_address"]["country_code"] == "66"
