"""SKU catalog used by profile-specific Meta Ads campaign naming.

The Ruza product sheet stores full product references such as
``ADB485.C2608`` while ad captions use the stable base code ``ADB485``.
This module keeps that normalization and the snapshot format in one small,
side-effect-free boundary so campaign planning does not need live Sheets
access.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


class AdsSkuCatalogError(ValueError):
    """Raised when a campaign SKU cannot be resolved safely."""


def normalize_sku_code(value: Any) -> str:
    """Return the base SKU before the sheet's optional ``.C2608`` suffix."""

    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    base = raw.split(".", 1)[0].strip()
    if not re.fullmatch(r"[A-Z0-9]+", base):
        raise AdsSkuCatalogError(f"SKU không hợp lệ: {value!r}")
    return base


def _product_record(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        source = str(value.get("source_product") or value.get("product") or value.get("sku") or "").strip()
        raw_sku = value.get("sku") or source
    else:
        source = str(value or "").strip()
        raw_sku = source
    sku = normalize_sku_code(raw_sku)
    if not sku:
        raise AdsSkuCatalogError(f"Dòng catalog thiếu SKU: {value!r}")
    return sku, source or sku


def build_snapshot(
    products: Iterable[Any],
    *,
    spreadsheet_id: str,
    sheet_name: str,
    column: str = "J",
    source_range: str = "J2:J1000",
    synced_at: str | None = None,
) -> dict[str, Any]:
    """Build a normalized, deterministic snapshot from Sheet column values."""

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in products:
        sku, source = _product_record(value)
        if sku in seen:
            continue
        seen.add(sku)
        normalized.append({"sku": sku, "source_product": source})
    if not normalized:
        raise AdsSkuCatalogError("Catalog SKU trống, không tạo snapshot.")
    return {
        "version": 1,
        "source": {
            "spreadsheet_id": str(spreadsheet_id).strip(),
            "sheet_name": str(sheet_name).strip(),
            "column": str(column).strip().upper(),
            "range": str(source_range).strip(),
        },
        "synced_at": synced_at or datetime.now(timezone.utc).isoformat(),
        "products": normalized,
        "sku_codes": [item["sku"] for item in normalized],
    }


def load_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdsSkuCatalogError(f"Không đọc được snapshot SKU {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AdsSkuCatalogError(f"Snapshot SKU hỏng {source}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdsSkuCatalogError("Snapshot SKU phải là một object JSON.")
    raw_products = payload.get("products")
    if not isinstance(raw_products, list):
        raw_products = payload.get("sku_codes")
    if not isinstance(raw_products, list):
        raise AdsSkuCatalogError("Snapshot SKU phải có mảng products hoặc sku_codes.")
    raw_source = payload.get("source")
    source = raw_source if isinstance(raw_source, Mapping) else {}
    return build_snapshot(
        raw_products,
        spreadsheet_id=str(source.get("spreadsheet_id", "")),
        sheet_name=str(source.get("sheet_name", "")),
        column=str(source.get("column", "J")),
        source_range=str(source.get("range", "J2:J1000")),
        synced_at=str(payload.get("synced_at") or "") or None,
    )


def catalog_codes(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    products = snapshot.get("products")
    if not isinstance(products, list):
        products = snapshot.get("sku_codes", [])
    codes: list[str] = []
    seen: set[str] = set()
    for item in products if isinstance(products, list) else []:
        sku, _ = _product_record(item)
        if sku not in seen:
            seen.add(sku)
            codes.append(sku)
    return tuple(codes)


def extract_catalog_codes(
    text: str,
    allowed_codes: Iterable[str],
    *,
    strict: bool = False,
) -> list[str]:
    """Extract known base codes, accepting ``#CODE`` and ``CODE.C2608``."""

    allowed = tuple(dict.fromkeys(normalize_sku_code(item) for item in allowed_codes if str(item).strip()))
    allowed = tuple(item for item in allowed if item)
    if not allowed:
        raise AdsSkuCatalogError("Catalog SKU chưa được cấu hình.")

    alternation = "|".join(re.escape(item) for item in sorted(allowed, key=len, reverse=True))
    known_pattern = re.compile(
        rf"(?<![A-Z0-9])#?(?P<code>{alternation})(?:\.[A-Z0-9]+)?(?![A-Z0-9])",
        re.IGNORECASE,
    )
    codes: list[str] = []
    seen: set[str] = set()
    for match in known_pattern.finditer(str(text or "")):
        code = normalize_sku_code(match.group("code"))
        if code not in seen:
            seen.add(code)
            codes.append(code)

    if strict:
        known = set(codes)
        unknown_pattern = re.compile(
            r"(?<![A-Z0-9])#?(?P<code>[A-Z]{2,}[0-9]{2,})(?:\.[A-Z0-9]+)?(?![A-Z0-9])",
            re.IGNORECASE,
        )
        unknown: list[str] = []
        for match in unknown_pattern.finditer(str(text or "")):
            code = normalize_sku_code(match.group("code"))
            if code not in known and code not in unknown:
                unknown.append(code)
        if unknown:
            raise AdsSkuCatalogError(
                "SKU không có trong catalog Ruza: " + ", ".join(unknown)
            )
    return codes


def write_snapshot(path: str | Path, snapshot: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
