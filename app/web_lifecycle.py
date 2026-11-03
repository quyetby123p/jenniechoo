from __future__ import annotations


WEB_LIFECYCLE_LABELS = {
    "ALL_AUDIENCES": "Thu hút lượt chuyển đổi từ tất cả đối tượng",
    "NEW_CUSTOMERS_ONLY": "Chinh phục khách hàng mới",
}


def normalize_web_lifecycle_strategy(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in WEB_LIFECYCLE_LABELS:
        raise ValueError(
            "Anh chưa chọn chiến lược vòng đời khách hàng. "
            "Anh chọn một trong hai nút trên Telegram trước khi tạo camp."
        )
    return normalized


def web_lifecycle_label(value: str) -> str:
    normalized = str(value or "").strip().upper()
    return WEB_LIFECYCLE_LABELS.get(normalized, normalized or "Chưa chọn")
