from __future__ import annotations

import re


_MAX_MEDIA_OWNER_LENGTH = 80


def normalize_media_owner(value: str) -> str:
    """Normalize the owner label used as the final ad-name segment."""

    owner = re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()
    if not owner:
        raise ValueError("Anh chưa gửi tên người làm media. Anh gửi tên người làm media giúp em.")
    if "|" in owner:
        raise ValueError("Tên người làm media không được chứa ký tự | vì đây là ký tự phân tách tên.")
    if len(owner) > _MAX_MEDIA_OWNER_LENGTH:
        raise ValueError(f"Tên người làm media tối đa {_MAX_MEDIA_OWNER_LENGTH} ký tự.")
    return owner
