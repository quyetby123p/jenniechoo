from __future__ import annotations

from contextlib import suppress
from datetime import date, datetime, timezone
import io
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from app.exceptions import MetaApiError, ValidationError
from app.models import AudienceSlot, PlannedCampaign, ResolvedPost
from app.settings import Settings
from app.utils import deep_merge, normalize_facebook_url, replace_placeholders
from app.web_lifecycle import normalize_web_lifecycle_strategy


class MetaAdsClient:
    _VIDEO_RESUMABLE_UPLOAD_THRESHOLD = 25 * 1024 * 1024
    _POST_VIDEO_MAX_BYTES = 500 * 1024 * 1024
    _OBJECTIVE_ALIASES = {
        "ENGAGEMENT": "OUTCOME_ENGAGEMENT",
        "AWARENESS": "OUTCOME_AWARENESS",
        "TRAFFIC": "OUTCOME_TRAFFIC",
        "LEADS": "OUTCOME_LEADS",
        "SALES": "OUTCOME_SALES",
        "APP_PROMOTION": "OUTCOME_APP_PROMOTION",
    }
    _BID_CONTROL_FIELDS = ("bid_amount", "cost_cap", "target_cost", "bid_constraints")
    _DEFAULT_MESSAGING_DESTINATION = "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
    _DEFAULT_MESSAGING_OPTIMIZATION = "MESSAGING_PURCHASE_CONVERSION"
    _CONTEXTUAL_MULTI_ADS_OPT_OUT = {"enroll_status": "OPT_OUT"}
    _LEGACY_RESULT_GOAL_TO_OPTIMIZATION = {
        "MAXIMIZE_PURCHASES_VIA_MESSAGE": _DEFAULT_MESSAGING_OPTIMIZATION,
    }
    _AUTO_DESTINATION_ERROR_MARKERS = (
        "degrees_of_freedom",
        "nội dung phải có thông số degrees_of_freedom",
        "nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó",
        "ad content is incompatible with the objective of the campaign",
        "application does not have the capability to make this api call",
    )
    _INSTAGRAM_MEDIA_REQUIREMENT_ERROR_MARKERS = (
        "bài viết của bạn không có hình ảnh hoặc video",
        "instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết",
        "không chứa từ 2-20 hình ảnh hoặc video",
        "khong chua tu 2-20 hinh anh hoac video",
        "your post has no image or video",
        "instagram ads currently only support video, image and link posts",
    )
    _LINK_AD_CTA_LOCK_ERROR_MARKERS = (
        "bài viết này đang chạy quảng cáo liên kết, do đó bạn chưa thể chỉnh sửa nút kêu gọi hành động",
        "bai viet nay dang chay quang cao lien ket, do do ban chua the chinh sua nut keu goi hanh dong",
        "this post is currently running link ads, so you can't edit the call to action button",
        "this post is currently running link ads, so you cannot edit the call to action button",
    )
    _POST_NOT_ADVERTISABLE_ERROR_MARKERS = (
        "bài viết này không thể đưa vào quảng cáo được",
        "quảng cáo bài viết không hợp lệ",
        "quang cao bai viet khong hop le",
        "this post cannot be used for an ad",
        "this post can't be used for an ad",
    )
    _PAGE_AD_ACCESS_ERROR_MARKERS = (
        "cần có quyền truy cập để quảng cáo cho trang này",
        "need access to advertise for this page",
        "request access to a page",
        "pages_read_engagement",
        "page public content access",
        "page public metadata access",
    )
    _AUTO_DESTINATION_DOF_SPEC = {
        "creative_features_spec": {
            "media_order": {"enroll_status": "OPT_IN"},
            "product_extensions": {
                "enroll_status": "OPT_IN",
                "customizations": {
                    "pe_carousel": {"enroll_status": "OPT_IN"},
                },
            },
            "text_optimizations": {"enroll_status": "OPT_IN"},
            "product_metadata_automation": {"enroll_status": "OPT_OUT"},
            "profile_card": {"enroll_status": "OPT_OUT"},
            "standard_enhancements_catalog": {"enroll_status": "OPT_OUT"},
            "video_to_image": {"enroll_status": "OPT_OUT"},
        }
    }
    _VIDEO_MEDIA_HINTS = ("video", "reel", "live")

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.base_url = f"https://graph.facebook.com/{settings.meta_api_version}"
        self.ad_account_id = settings.meta_ad_account_id
        if not self.ad_account_id.startswith("act_"):
            self.ad_account_id = f"act_{self.ad_account_id}"
        self._page_token_owner_id: str | None = None
        self._creative_access_token_checked = False
        self._creative_access_token_cache = ""

    def _encode_data(self, payload: dict[str, Any]) -> dict[str, str]:
        encoded: dict[str, str] = {}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                encoded[key] = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                encoded[key] = "true" if value else "false"
            else:
                encoded[key] = str(value)
        return encoded

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        request_data = dict(data or {})
        request_params["access_token"] = access_token or self.settings.meta_access_token

        attempts = max(1, self.settings.retry_max)
        for attempt in range(1, attempts + 1):
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    params=request_params,
                    data=self._encode_data(request_data) if request_data else None,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise MetaApiError(f"Loi ket noi Meta API: {exc}") from exc
                self._sleep_for_retry(attempt)
                continue

            if response.status_code < 400:
                return self._json_or_raise(response.text)

            retryable = response.status_code in {429, 500, 502, 503, 504}
            if retryable and attempt < attempts:
                self._sleep_for_retry(attempt)
                continue

            error_message = self._extract_error_message(response.text)
            raise MetaApiError(f"Meta API loi ({response.status_code}): {error_message}")

        raise MetaApiError("Meta API loi khong xac dinh.")

    def _get_creative_access_token(self) -> str | None:
        if self._creative_access_token_checked:
            return self._creative_access_token_cache or None
        self._creative_access_token_checked = True
        token = str(getattr(self.settings, "meta_creative_access_token", "") or "").strip()
        if not token:
            return None
        self._creative_access_token_cache = token
        return token

    def _multipart_request(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, Any] | None = None,
        timeout_seconds: int = 120,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Send a binary upload using the same retry policy as Graph requests."""

        url = f"{self.base_url}{path}"
        request_params = {"access_token": access_token or self.settings.meta_access_token}
        request_data = self._encode_data(dict(data or {}))
        attempts = max(1, self.settings.retry_max)
        for attempt in range(1, attempts + 1):
            try:
                multipart_files = {
                    key: (filename, io.BytesIO(content), mime_type)
                    for key, (filename, content, mime_type) in files.items()
                }
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    params=request_params,
                    data=request_data,
                    files=multipart_files,
                    timeout=max(30, int(timeout_seconds)),
                )
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise MetaApiError(f"Loi ket noi Meta API khi upload media: {exc}") from exc
                self._sleep_for_retry(attempt)
                continue

            if response.status_code < 400:
                return self._json_or_raise(response.text)
            retryable = response.status_code in {429, 500, 502, 503, 504}
            if retryable and attempt < attempts:
                self._sleep_for_retry(attempt)
                continue
            error_message = self._extract_error_message(response.text)
            raise MetaApiError(f"Meta API loi upload media ({response.status_code}): {error_message}")

        raise MetaApiError("Meta API loi khong xac dinh khi upload media.")

    def upload_image_asset(self, content: bytes, filename: str = "creative.jpg") -> str:
        if not content:
            raise ValidationError("Ảnh Telegram tải về đang trống.")
        payload = self._multipart_request(
            "POST",
            f"/{self.ad_account_id}/adimages",
            files={"filename": (filename or "creative.jpg", content, "image/jpeg")},
        )
        images = payload.get("images") if isinstance(payload, dict) else None
        if isinstance(images, dict):
            for item in images.values():
                if isinstance(item, dict) and str(item.get("hash", "")).strip():
                    return str(item["hash"]).strip()
        image_hash = str(payload.get("hash", "")).strip() if isinstance(payload, dict) else ""
        if image_hash:
            return image_hash
        raise MetaApiError("Meta không trả về image_hash sau khi upload ảnh.")

    def upload_video_asset(self, content: bytes, filename: str = "creative.mp4") -> str:
        if not content:
            raise ValidationError("Video Telegram tải về đang trống.")
        safe_filename = filename or "creative.mp4"
        if len(content) >= self._VIDEO_RESUMABLE_UPLOAD_THRESHOLD:
            return self._upload_video_asset_resumable(content, safe_filename)
        try:
            payload = self._multipart_request(
                "POST",
                f"/{self.ad_account_id}/advideos",
                files={"source": (safe_filename, content, "video/mp4")},
            )
        except MetaApiError as exc:
            # Some Graph/API proxies reject a single multipart body before Meta
            # can return its normal upload error. Retry through upload sessions.
            if "413" not in str(exc) and "entity too large" not in str(exc).lower():
                raise
            return self._upload_video_asset_resumable(content, safe_filename)
        video_id = str(payload.get("id", payload.get("video_id", ""))).strip() if isinstance(payload, dict) else ""
        if not video_id:
            raise MetaApiError("Meta không trả về video_id sau khi upload video.")
        return video_id

    def _upload_video_asset_resumable(self, content: bytes, filename: str) -> str:
        path = f"/{self.ad_account_id}/advideos"
        start_payload = self._request(
            "POST",
            path,
            data={"upload_phase": "start", "file_size": len(content)},
        )
        session_id = str(start_payload.get("upload_session_id", "")).strip()
        video_id = str(start_payload.get("video_id", "")).strip()
        if not session_id:
            raise MetaApiError("Meta không trả về upload_session_id cho video lớn.")

        start_offset = int(start_payload.get("start_offset", 0) or 0)
        end_offset = int(start_payload.get("end_offset", 0) or 0)
        if end_offset <= start_offset:
            end_offset = min(len(content), start_offset + 10 * 1024 * 1024)

        while start_offset < len(content):
            chunk_end = min(len(content), end_offset)
            if chunk_end <= start_offset:
                raise MetaApiError("Meta trả về khoảng chunk video không hợp lệ.")
            chunk = content[start_offset:chunk_end]
            transfer_payload = self._multipart_request(
                "POST",
                path,
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": start_offset,
                },
                files={"video_file_chunk": (filename, chunk, "video/mp4")},
                timeout_seconds=300,
            )
            next_start = int(transfer_payload.get("start_offset", chunk_end) or chunk_end)
            next_end = int(transfer_payload.get("end_offset", 0) or 0)
            if next_start <= start_offset:
                raise MetaApiError("Meta không xác nhận được chunk video vừa tải lên.")
            start_offset = next_start
            end_offset = next_end if next_end > start_offset else min(len(content), start_offset + 10 * 1024 * 1024)

        finish_payload = self._request(
            "POST",
            path,
            data={
                "upload_phase": "finish",
                "upload_session_id": session_id,
                "title": filename,
            },
        )
        video_id = video_id or str(finish_payload.get("id", finish_payload.get("video_id", ""))).strip()
        if not video_id:
            raise MetaApiError("Meta không trả về video_id sau khi hoàn tất upload video lớn.")
        return video_id

    def upload_resolved_post_video(self, resolved_post: ResolvedPost) -> str:
        """Upload an inline Reel once so messaging ads do not reuse an incompatible story ID."""

        source_url = str(resolved_post.video_source_url or "").strip()
        if not source_url:
            raise ValidationError(
                "Meta không trả về link tải video của Reel này. "
                "Anh thử gửi một Reel khác hoặc upload trực tiếp video cho bot."
            )

        response = None
        try:
            response = requests.get(source_url, stream=True, timeout=120)
            response.raise_for_status()
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > self._POST_VIDEO_MAX_BYTES:
                raise ValidationError(
                    f"Video Reel vượt giới hạn an toàn {self._POST_VIDEO_MAX_BYTES // (1024 * 1024)} MB."
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._POST_VIDEO_MAX_BYTES:
                    raise ValidationError(
                        f"Video Reel vượt giới hạn an toàn {self._POST_VIDEO_MAX_BYTES // (1024 * 1024)} MB."
                    )
                chunks.append(chunk)
        except ValidationError:
            raise
        except requests.RequestException as exc:
            raise MetaApiError(f"Không tải được video Reel từ Meta: {exc}") from exc
        finally:
            if response is not None:
                response.close()

        content = b"".join(chunks)
        if not content:
            raise ValidationError("Meta trả về video Reel rỗng.")
        video_id = str(resolved_post.video_id or "reel").strip()
        self.logger.info(
            "Uploading inline Reel video %s to ad account (%d bytes)",
            video_id,
            len(content),
        )
        return self.upload_video_asset(content, f"reel_{video_id}.mp4")

    def _media_read_access_tokens(self) -> list[str]:
        candidates = [
            str(self.settings.meta_access_token or "").strip(),
            str(self.settings.meta_page_access_token or "").strip(),
            str(getattr(self.settings, "meta_creative_access_token", "") or "").strip(),
        ]
        tokens: list[str] = []
        seen: set[str] = set()
        for token in candidates:
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @staticmethod
    def _should_retry_media_read_token(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "token has expired",
                "session has expired",
                "error validating access token",
                "invalid oauth access token",
                "access token",
                "permissions error",
                "requires ads_read",
                "does not have permission",
            )
        )

    def _request_media_read(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tokens = self._media_read_access_tokens()
        if not tokens:
            return self._request("GET", path, params=params, access_token=self.settings.meta_access_token)

        last_error: MetaApiError | None = None
        for index, token in enumerate(tokens):
            try:
                return self._request("GET", path, params=params, access_token=token)
            except MetaApiError as exc:
                last_error = exc
                has_next_token = index < len(tokens) - 1
                if not has_next_token or not self._should_retry_media_read_token(exc):
                    raise
                self.logger.warning(
                    "Meta read token failed for %s; retrying with fallback token: %s",
                    path,
                    str(exc)[:300],
                )

        if last_error is not None:
            raise last_error
        raise MetaApiError("Meta API loi khong xac dinh khi doc media.")

    def _sleep_for_retry(self, attempt: int) -> None:
        import time

        if attempt - 1 < len(self.settings.retry_backoff_seconds):
            delay = self.settings.retry_backoff_seconds[attempt - 1]
        else:
            delay = self.settings.retry_backoff_seconds[-1]
        time.sleep(max(0, delay))

    @staticmethod
    def _json_or_raise(raw_text: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise MetaApiError(f"Meta API tra ve JSON khong hop le: {raw_text}") from exc
        return payload

    @staticmethod
    def _extract_error_message(raw_text: str) -> str:
        try:
            payload = json.loads(raw_text)
            error = payload.get("error", {})
            if isinstance(error, dict):
                subcode = str(error.get("error_subcode", "")).strip()
                if subcode == "1885183":
                    return (
                        "Meta đang chặn tạo ad creative vì ứng dụng cấp token vẫn ở Development mode "
                        "(mã 1885183). Anh chuyển ứng dụng Meta sang Live/Public, sau đó dùng system user "
                        "token của ứng dụng đó và chạy lại lệnh."
                    )
                message = error.get("error_user_msg") or error.get("message")
                if message:
                    details: list[str] = []
                    code = str(error.get("code", "")).strip()
                    if code:
                        details.append(f"code={code}")
                    if subcode:
                        details.append(f"subcode={subcode}")
                    suffix = f" ({', '.join(details)})" if details else ""
                    return f"{message}{suffix}"
        except json.JSONDecodeError:
            pass
        return raw_text[:500]

    def check_token_health(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "ok": True,
            "checks": {},
        }

        checks: dict[str, Any] = {}

        try:
            payload = self._request(
                "GET",
                f"/{self.ad_account_id}",
                params={"fields": "id,account_status,currency,name"},
                access_token=self.settings.meta_access_token,
            )
            checks["ads_account"] = {
                "ok": True,
                "id": str(payload.get("id", "")),
                "name": str(payload.get("name", "")),
                "account_status": str(payload.get("account_status", "")),
                "currency": str(payload.get("currency", "")),
            }
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            checks["ads_account"] = {
                "ok": False,
                "error": str(exc),
            }

        try:
            payload = self._request(
                "GET",
                "/me",
                params={"fields": "id,name"},
                access_token=self.settings.meta_access_token,
            )
            checks["ads_identity"] = {
                "ok": True,
                "id": str(payload.get("id", "")),
                "name": str(payload.get("name", "")),
            }
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            checks["ads_identity"] = {
                "ok": False,
                "error": str(exc),
            }

        try:
            payload = self._request(
                "GET",
                f"/{self.settings.meta_page_id}",
                params={"fields": "id,name"},
                access_token=self.settings.meta_access_token,
            )
            checks["ads_page_access"] = {
                "ok": True,
                "id": str(payload.get("id", "")),
                "name": str(payload.get("name", "")),
            }
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            error_text = str(exc)
            checks["ads_page_access"] = {
                "ok": False,
                "error": error_text,
            }
            if self.is_page_ad_access_error(error_text):
                checks["ads_page_access"]["hint"] = self._page_ad_access_fix_message()

        page_token = self.settings.meta_page_access_token.strip()
        if page_token:
            try:
                payload = self._request(
                    "GET",
                    "/me",
                    params={"fields": "id,name"},
                    access_token=page_token,
                )
                checks["page_identity"] = {
                    "ok": True,
                    "id": str(payload.get("id", "")),
                    "name": str(payload.get("name", "")),
                }
                if str(payload.get("id", "")).strip() != str(self.settings.meta_page_id).strip():
                    report["ok"] = False
                    checks["page_identity"]["ok"] = False
                    checks["page_identity"]["error"] = (
                        f"Token trang dang tro toi page_id={payload.get('id')} "
                        f"nhung cau hinh la META_PAGE_ID={self.settings.meta_page_id}"
                    )
            except Exception as exc:  # noqa: BLE001
                report["ok"] = False
                checks["page_identity"] = {
                    "ok": False,
                    "error": str(exc),
                }

            try:
                payload = self._request(
                    "GET",
                    f"/{self.settings.meta_page_id}/posts",
                    params={"fields": "id", "limit": 1},
                    access_token=page_token,
                )
                first_post_id = ""
                data = payload.get("data", [])
                if isinstance(data, list) and data:
                    first_post_id = str(data[0].get("id", ""))
                checks["page_posts"] = {
                    "ok": True,
                    "first_post_id": first_post_id,
                }
            except Exception as exc:  # noqa: BLE001
                report["ok"] = False
                checks["page_posts"] = {
                    "ok": False,
                    "error": str(exc),
                }
        else:
            report["ok"] = False
            checks["page_identity"] = {
                "ok": False,
                "error": "Chua cau hinh META_PAGE_ACCESS_TOKEN.",
            }
            checks["page_posts"] = {
                "ok": False,
                "error": "Chua cau hinh META_PAGE_ACCESS_TOKEN.",
            }

        report["checks"] = checks
        return report

    def ensure_ads_token_can_access_page(self) -> None:
        try:
            self._request(
                "GET",
                f"/{self.settings.meta_page_id}",
                params={"fields": "id,name"},
                access_token=self.settings.meta_access_token,
            )
        except MetaApiError as exc:
            if self.is_page_ad_access_error(str(exc)):
                raise ValidationError(self._page_ad_access_fix_message()) from exc
            raise

    def ensure_pixel_can_access(self, pixel_id: str | None = None) -> dict[str, Any]:
        normalized_pixel_id = str(pixel_id or self.settings.meta_pixel_id).strip()
        if not normalized_pixel_id:
            raise ValidationError("Thiếu META_PIXEL_ID để chạy camp Web tối ưu Purchase.")
        payload = self._request(
            "GET",
            f"/{normalized_pixel_id}",
            params={"fields": "id,name"},
        )
        returned_id = str(payload.get("id", "")).strip()
        if returned_id and returned_id != normalized_pixel_id:
            raise ValidationError(
                f"Pixel trả về ID {returned_id}, không khớp META_PIXEL_ID={normalized_pixel_id}."
            )
        return payload

    def diagnose_instagram_post_access(self, object_story_id: str) -> dict[str, Any]:
            diagnostics: dict[str, Any] = {
                "is_instagram_origin": False,
                "instagram_eligibility": "",
                "is_instagram_eligible": False,
                "instagram_basic_granted": False,
                "instagram_account_count": 0,
                "creative_token_present": bool(self._get_creative_access_token()),
                "creative_instagram_basic_granted": False,
                "creative_instagram_account_count": 0,
                "has_instagram_media_access": False,
                "errors": [],
            }
            normalized_story_id = str(object_story_id or "").strip()
            if normalized_story_id:
                try:
                    post = self._request(
                        "GET",
                        f"/{normalized_story_id}",
                        params={
                            "fields": "id,admin_creator,instagram_eligibility,is_instagram_eligible",
                        },
                        access_token=(
                            self.settings.meta_page_access_token.strip()
                            or self.settings.meta_access_token
                        ),
                    )
                    admin_creator = post.get("admin_creator")
                    if isinstance(admin_creator, dict):
                        creator_name = str(admin_creator.get("name", "")).strip().lower()
                        creator_namespace = str(admin_creator.get("namespace", "")).strip().lower()
                        diagnostics["is_instagram_origin"] = (
                            creator_name == "instagram"
                            or creator_namespace in {"instagram", "instapp"}
                        )
                    diagnostics["instagram_eligibility"] = str(
                        post.get("instagram_eligibility", "")
                    ).strip()
                    diagnostics["is_instagram_eligible"] = bool(
                        post.get("is_instagram_eligible", False)
                    )
                except Exception as exc:  # noqa: BLE001
                    diagnostics["errors"].append(f"post: {exc}")

            checked_tokens = [
                ("ads", self.settings.meta_access_token),
            ]
            creative_token = self._get_creative_access_token()
            if creative_token:
                checked_tokens.append(("creative", creative_token))

            for label, token in checked_tokens:
                basic_granted = False
                account_count = 0
                try:
                    permissions = self._request(
                        "GET",
                        "/me/permissions",
                        access_token=token,
                    )
                    granted = {
                        str(item.get("permission", "")).strip()
                        for item in permissions.get("data", [])
                        if isinstance(item, dict)
                        and str(item.get("status", "")).strip().lower() == "granted"
                    }
                    basic_granted = bool(
                        {"instagram_basic", "instagram_business_basic"} & granted
                    )
                except Exception as exc:  # noqa: BLE001
                    diagnostics["errors"].append(f"{label}_permissions: {exc}")

                try:
                    accounts = self._request(
                        "GET",
                        f"/{self.ad_account_id}/instagram_accounts",
                        params={"fields": "id,username", "limit": 100},
                        access_token=token,
                    )
                    data = accounts.get("data", [])
                    account_count = len(data) if isinstance(data, list) else 0
                except Exception as exc:  # noqa: BLE001
                    diagnostics["errors"].append(f"{label}_instagram_accounts: {exc}")

                if label == "creative":
                    diagnostics["creative_instagram_basic_granted"] = bool(basic_granted)
                    diagnostics["creative_instagram_account_count"] = max(
                        int(diagnostics["creative_instagram_account_count"]),
                        account_count,
                    )

                diagnostics["instagram_basic_granted"] = bool(
                    diagnostics["instagram_basic_granted"] or basic_granted
                )
                diagnostics["instagram_account_count"] = max(
                    int(diagnostics["instagram_account_count"]),
                    account_count,
                )

            diagnostics["has_instagram_media_access"] = bool(
                diagnostics["instagram_basic_granted"]
                and diagnostics["instagram_account_count"] > 0
            )
            return diagnostics

    def get_daily_spend(self, report_date: date, timezone_name: str) -> dict[str, Any]:
        result = self.get_spend_for_range(report_date, report_date, timezone_name)
        return {
            **result,
            "report_date": report_date.isoformat(),
        }

    def get_spend_for_range(self, start_date: date, end_date: date, timezone_name: str) -> dict[str, Any]:
        del timezone_name  # Meta insights nhận time_range theo ngày; timezone dùng theo ad account.
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        time_range = {
            "since": start_date.isoformat(),
            "until": end_date.isoformat(),
        }
        payload = self._request_media_read(
            f"/{self.ad_account_id}/insights",
            params={
                "fields": "spend,account_id,date_start,date_stop",
                "level": "account",
                "time_range": json.dumps(time_range, ensure_ascii=False),
            },
        )
        data = payload.get("data", [])
        if not isinstance(data, list) or not data:
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "account_id": self.ad_account_id,
                "date_start": start_date.isoformat(),
                "date_stop": end_date.isoformat(),
                "spend_vnd": 0,
                "currency": self.settings.app_currency,
            }

        rows = [item for item in data if isinstance(item, dict)]
        if not rows:
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "account_id": self.ad_account_id,
                "date_start": start_date.isoformat(),
                "date_stop": end_date.isoformat(),
                "spend_vnd": 0,
                "currency": self.settings.app_currency,
            }

        spend_vnd = sum(self._to_vnd_int(str(row.get("spend", "0")).strip()) for row in rows)
        first = rows[0]
        last = rows[-1]
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "account_id": str(first.get("account_id", self.ad_account_id)),
            "date_start": str(first.get("date_start", start_date.isoformat())),
            "date_stop": str(last.get("date_stop", end_date.isoformat())),
            "spend_vnd": spend_vnd,
            "currency": self.settings.app_currency,
        }

    def get_ad_insights_for_range(
        self,
        start_date: date,
        end_date: date,
        timezone_name: str,
        *,
        max_rows: int = 5000,
    ) -> list[dict[str, Any]]:
        del timezone_name  # Meta insights nhận time_range theo ngày; timezone dùng theo ad account.
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        time_range = {
            "since": start_date.isoformat(),
            "until": end_date.isoformat(),
        }
        next_path = f"/{self.ad_account_id}/insights"
        next_params: dict[str, Any] | None = {
            "fields": ",".join(
                [
                    "campaign_id",
                    "campaign_name",
                    "adset_id",
                    "adset_name",
                    "ad_id",
                    "ad_name",
                    "spend",
                    "impressions",
                    "reach",
                    "clicks",
                    "inline_link_clicks",
                    "actions",
                    "action_values",
                    "video_play_actions",
                    "video_thruplay_watched_actions",
                    "date_start",
                    "date_stop",
                ]
            ),
            "level": "ad",
            "time_range": json.dumps(time_range, ensure_ascii=False),
            "use_unified_attribution_setting": "true",
            "limit": 500,
        }
        rows: list[dict[str, Any]] = []
        while next_path and len(rows) < max(1, int(max_rows)):
            payload = self._request_media_read(
                next_path,
                params=next_params,
            )
            data = payload.get("data", [])
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        rows.append(item)
                        if len(rows) >= max(1, int(max_rows)):
                            break
            paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
            next_url = str(paging.get("next", "")).strip()
            if not next_url:
                break
            next_path, next_params = self._path_and_params_from_next_url(next_url)
        return rows

    def get_ads_metadata(self, ad_ids: list[str]) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for ad_id in ad_ids:
            normalized_ad_id = str(ad_id).strip()
            if not normalized_ad_id or normalized_ad_id in seen:
                continue
            seen.add(normalized_ad_id)
            try:
                payload = self._request(
                    "GET",
                    f"/{normalized_ad_id}",
                    params={
                        "fields": (
                            "id,name,status,effective_status,updated_time,created_time,"
                            "adset{id,name},campaign{id,name},"
                            "creative{id,name,object_story_id,effective_object_story_id,thumbnail_url,url_tags},"
                            "issues_info"
                        ),
                    },
                    access_token=self.settings.meta_access_token,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Khong doc duoc metadata ad %s: %s", normalized_ad_id, exc)
                continue
            creative = payload.get("creative") if isinstance(payload.get("creative"), dict) else {}
            adset = payload.get("adset") if isinstance(payload.get("adset"), dict) else {}
            campaign = payload.get("campaign") if isinstance(payload.get("campaign"), dict) else {}
            metadata[normalized_ad_id] = {
                "ad_id": str(payload.get("id", normalized_ad_id)).strip(),
                "ad_name": str(payload.get("name", "")).strip(),
                "status": str(payload.get("status", "")).strip(),
                "effective_status": str(payload.get("effective_status", "")).strip(),
                "updated_time": str(payload.get("updated_time", "")).strip(),
                "created_time": str(payload.get("created_time", "")).strip(),
                "adset_id": str(adset.get("id", "")).strip(),
                "adset_name": str(adset.get("name", "")).strip(),
                "campaign_id": str(campaign.get("id", "")).strip(),
                "campaign_name": str(campaign.get("name", "")).strip(),
                "creative_id": str(creative.get("id", "")).strip(),
                "creative_name": str(creative.get("name", "")).strip(),
                "object_story_id": str(creative.get("object_story_id", "")).strip(),
                "effective_object_story_id": str(creative.get("effective_object_story_id", "")).strip(),
                "thumbnail_url": str(creative.get("thumbnail_url", "")).strip(),
                "url_tags": str(creative.get("url_tags", "")).strip(),
                "issues_info": payload.get("issues_info", []),
            }
        return metadata

    def get_post_metadata_for_story_ids(self, story_ids: list[str]) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        access_token = self.settings.meta_page_access_token or self.settings.meta_access_token
        for story_id in story_ids:
            normalized_story_id = str(story_id).strip()
            if not normalized_story_id or normalized_story_id in seen:
                continue
            seen.add(normalized_story_id)
            try:
                payload = self._request(
                    "GET",
                    f"/{normalized_story_id}",
                    params={
                        "fields": "id,message,permalink_url,created_time,attachments{media_type,type,url,target}",
                    },
                    access_token=access_token,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Khong doc duoc post metadata %s: %s", normalized_story_id, exc)
                continue
            metadata[normalized_story_id] = {
                "id": str(payload.get("id", normalized_story_id)).strip(),
                "message": str(payload.get("message", "")).strip(),
                "permalink_url": str(payload.get("permalink_url", "")).strip(),
                "created_time": str(payload.get("created_time", "")).strip(),
                "attachments": payload.get("attachments", {}),
            }
        return metadata

    def list_page_posts_by_sku(
            self,
            sku_codes: list[str],
            *,
            max_scan: int = 1000,
            max_posts: int = 20,
        ) -> list[ResolvedPost]:
            normalized_codes: list[str] = []
            seen_codes: set[str] = set()
            for raw_code in sku_codes:
                code = str(raw_code or "").strip().upper()
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                normalized_codes.append(code)
            if not normalized_codes:
                return []
            if not self.settings.meta_page_access_token:
                raise ValidationError(
                    "Chưa cấu hình `META_PAGE_ACCESS_TOKEN`, nên bot chưa quét được các bài cùng SKU trên Page."
                )

            self._validate_page_access_token_owner()
            patterns = [
                re.compile(
                    rf"(?<![0-9A-Z])#?\s*{re.escape(code)}(?![0-9A-Z])",
                    re.IGNORECASE,
                )
                for code in normalized_codes
            ]
            next_path = f"/{self.settings.meta_page_id}/posts"
            next_params: dict[str, Any] | None = {
                "fields": (
                    "id,message,permalink_url,status_type,created_time,"
                    "attachments{media_type,type,subattachments{media_type,type}}"
                ),
                "limit": 100,
            }
            scanned = 0
            posts: list[ResolvedPost] = []
            seen_story_ids: set[str] = set()
            try:
                while next_path and scanned < max(1, int(max_scan)) and len(posts) < max(1, int(max_posts)):
                    payload = self._request(
                        "GET",
                        next_path,
                        params=next_params,
                        access_token=self.settings.meta_page_access_token,
                    )
                    data = payload.get("data", [])
                    if not isinstance(data, list):
                        data = []

                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        scanned += 1
                        message_text = str(item.get("message", "")).strip()
                        if not message_text or not all(pattern.search(message_text) for pattern in patterns):
                            continue
                        post_node_id = str(item.get("id", "")).strip()
                        if not post_node_id:
                            continue
                        if "_" in post_node_id:
                            owner_page_id, post_id = post_node_id.split("_", 1)
                        else:
                            owner_page_id = self.settings.meta_page_id
                            post_id = post_node_id
                        self._validate_owner_page(owner_page_id)
                        object_story_id = f"{owner_page_id}_{post_id}"
                        if object_story_id in seen_story_ids:
                            continue
                        seen_story_ids.add(object_story_id)
                        permalink = str(item.get("permalink_url", "")).strip()
                        posts.append(
                            ResolvedPost(
                                post_id=post_id,
                                page_id=owner_page_id,
                                permalink_url=permalink,
                                object_story_id=object_story_id,
                                strategy="page_sku_scan",
                                message_text=message_text,
                                media_label=self._detect_media_label(permalink, item),
                            )
                        )
                        if len(posts) >= max(1, int(max_posts)):
                            break

                    paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
                    next_url = str(paging.get("next", "")).strip()
                    if not next_url:
                        break
                    next_path, next_params = self._path_and_params_from_next_url(next_url)
            except MetaApiError as exc:
                msg = str(exc).lower()
                if "mã truy cập trang" in msg or "page access token" in msg or "user access token is not supported" in msg:
                    raise ValidationError(
                        "Token hiện tại chưa có quyền đọc bài viết của Trang để quét các post cùng SKU."
                    ) from exc
                raise
            return posts

    def resolve_post(self, post_url: str) -> ResolvedPost:
        normalized_url = normalize_facebook_url(post_url)
        direct = self._resolve_post_from_url_patterns(normalized_url)
        if direct:
            return direct

        if not self.settings.meta_page_access_token:
            raise ValidationError(
                "Link bài viết đang ở dạng pfbid nên Meta không resolve trực tiếp được.\n"
                "Anh cần thêm `META_PAGE_ACCESS_TOKEN` (Page Access Token) vào file .env, "
                "hoặc gửi link dạng `permalink.php?story_fbid=...&id=...`."
            )

        direct_from_pfbid = self._resolve_post_from_pageid_pfbid(normalized_url)
        if direct_from_pfbid:
            return direct_from_pfbid

        return self._resolve_post_from_page_posts(normalized_url)

    def _resolve_post_from_url_patterns(self, post_url: str) -> ResolvedPost | None:
        parsed = urlparse(post_url)
        query = parse_qs(parsed.query)

        story_fbid = self._first(query, "story_fbid", "fbid")
        owner_id = self._first(query, "id")
        if story_fbid and owner_id:
            self._validate_owner_page(owner_id)
            return self._enrich_resolved_post(ResolvedPost(
                post_id=story_fbid,
                page_id=owner_id,
                permalink_url=post_url,
                object_story_id=f"{owner_id}_{story_fbid}",
                strategy="direct_story_fbid",
            ))

        path_match = re.search(r"/posts/(?P<post_id>\d+)$", parsed.path or "", re.IGNORECASE)
        if path_match:
            post_id = path_match.group("post_id")
            owner_page_id = self.settings.meta_page_id
            return self._enrich_resolved_post(ResolvedPost(
                post_id=post_id,
                page_id=owner_page_id,
                permalink_url=post_url,
                object_story_id=f"{owner_page_id}_{post_id}",
                strategy="direct_numeric_post",
            ))
        return None

    def _resolve_post_from_page_posts(self, post_url: str) -> ResolvedPost:
        self._validate_page_access_token_owner()
        pfbid_token = self._extract_pfbid_token(post_url)
        page_path = f"/{self.settings.meta_page_id}/posts"
        params: dict[str, Any] = {
            "fields": "id,permalink_url",
            "limit": 100,
        }

        next_path = page_path
        next_params = params
        scanned = 0

        try:
            while next_path and scanned < 1000:
                payload = self._request(
                    "GET",
                    next_path,
                    params=next_params,
                    access_token=self.settings.meta_page_access_token,
                )
                data = payload.get("data", [])
                if not isinstance(data, list):
                    data = []

                for item in data:
                    scanned += 1
                    post_node_id = str(item.get("id", "")).strip()
                    permalink = str(item.get("permalink_url", "")).strip()
                    if not post_node_id:
                        continue
                    if self._is_permalink_match(post_url, permalink, pfbid_token):
                        return self._to_resolved_post(
                            post_node_id,
                            permalink or post_url,
                            strategy="matched_page_permalink",
                        )

                paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
                next_url = str(paging.get("next", "")).strip()
                if not next_url:
                    break
                next_path, next_params = self._path_and_params_from_next_url(next_url)
        except MetaApiError as exc:
            msg = str(exc).lower()
            if "mã truy cập trang" in msg or "page access token" in msg or "user access token is not supported" in msg:
                raise ValidationError(
                    "Token hiện tại chưa có quyền đọc bài viết của Trang.\n"
                    "Anh vui lòng cấp `META_PAGE_ACCESS_TOKEN` (Page Access Token) để bot tìm post ID từ link pfbid."
                ) from exc
            raise

        raise ValidationError(
            "Không map được link pfbid sang đúng bài viết trong feed của Trang; bot dừng để tránh lên nhầm bài.\n"
            "Anh gửi link permalink đầy đủ hoặc kiểm tra lại quyền Page Access Token."
        )

    def _resolve_post_from_pageid_pfbid(self, post_url: str) -> ResolvedPost | None:
        pfbid_token = self._extract_pfbid_token(post_url)
        if not pfbid_token:
            return None
        self._validate_page_access_token_owner()
        path = f"/{self.settings.meta_page_id}_{pfbid_token}"
        try:
            payload = self._request(
                "GET",
                path,
                params={"fields": "id,permalink_url"},
                access_token=self.settings.meta_page_access_token,
            )
        except MetaApiError:
            return None
        post_node_id = str(payload.get("id", "")).strip()
        if not post_node_id:
            return None
        permalink = str(payload.get("permalink_url", "")).strip() or post_url
        return self._to_resolved_post(
            post_node_id,
            permalink,
            strategy="direct_pageid_pfbid",
        )

    def _validate_page_access_token_owner(self) -> None:
        if self._page_token_owner_id is not None:
            owner_id = self._page_token_owner_id
            if owner_id != self.settings.meta_page_id:
                raise ValidationError(
                    "META_PAGE_ACCESS_TOKEN không khớp META_PAGE_ID.\n"
                    f"Token hiện tại thuộc page_id={owner_id}, "
                    f"nhưng cấu hình đang là META_PAGE_ID={self.settings.meta_page_id}."
                )
            return

        payload = self._request(
            "GET",
            "/me",
            params={"fields": "id,name"},
            access_token=self.settings.meta_page_access_token,
        )
        owner_id = str(payload.get("id", "")).strip()
        if not owner_id:
            raise ValidationError(
                "Không đọc được thông tin từ META_PAGE_ACCESS_TOKEN. Anh kiểm tra lại token trang."
            )
        self._page_token_owner_id = owner_id
        if owner_id != self.settings.meta_page_id:
            raise ValidationError(
                "META_PAGE_ACCESS_TOKEN không khớp META_PAGE_ID.\n"
                f"Token hiện tại thuộc page_id={owner_id}, "
                f"nhưng cấu hình đang là META_PAGE_ID={self.settings.meta_page_id}."
            )

    def _to_resolved_post(self, post_node_id: str, permalink_url: str, strategy: str) -> ResolvedPost:
        if "_" in post_node_id:
            owner_page_id, post_id = post_node_id.split("_", 1)
        else:
            owner_page_id = self.settings.meta_page_id
            post_id = post_node_id
        self._validate_owner_page(owner_page_id)
        resolved = ResolvedPost(
            post_id=post_id,
            page_id=owner_page_id,
            permalink_url=permalink_url,
            object_story_id=f"{owner_page_id}_{post_id}",
            strategy=strategy,
        )
        return self._enrich_resolved_post(resolved)

    def _enrich_resolved_post(self, resolved_post: ResolvedPost) -> ResolvedPost:
        if not self.settings.meta_page_access_token:
            return resolved_post

        node_id = resolved_post.object_story_id
        try:
            details = self._request(
                "GET",
                f"/{node_id}",
                params={
                    "fields": (
                        "id,message,permalink_url,status_type,"
                        "attachments{media_type,type,target,subattachments{media_type,type,target}}"
                    )
                },
                access_token=self.settings.meta_page_access_token,
            )
        except Exception:  # noqa: BLE001
            return resolved_post

        message_text = str(details.get("message", "")).strip()
        permalink = str(details.get("permalink_url", "")).strip() or resolved_post.permalink_url
        media_label = self._detect_media_label(permalink, details)
        video_id = ""
        video_source_url = ""
        video_thumbnail_url = ""
        video_attachment_type = ""
        video_attachment = self._find_video_attachment(details.get("attachments"))
        if video_attachment:
            video_attachment_type = str(video_attachment.get("type", "")).strip().lower()
            target = video_attachment.get("target")
            if isinstance(target, dict):
                video_id = str(target.get("id", "")).strip()
            if video_attachment_type == "video_inline" and video_id:
                try:
                    video_payload = self._request(
                        "GET",
                        f"/{video_id}",
                        params={"fields": "id,source,picture"},
                        access_token=self.settings.meta_page_access_token,
                    )
                    video_source_url = str(video_payload.get("source", "")).strip()
                    video_thumbnail_url = str(video_payload.get("picture", "")).strip()
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "Khong lay duoc source video cho inline Reel %s: %s",
                        video_id,
                        exc,
                    )
        return ResolvedPost(
            post_id=resolved_post.post_id,
            page_id=resolved_post.page_id,
            permalink_url=permalink,
            object_story_id=resolved_post.object_story_id,
            strategy=resolved_post.strategy,
            message_text=message_text,
            media_label=media_label,
            video_id=video_id,
            video_source_url=video_source_url,
            video_thumbnail_url=video_thumbnail_url,
            video_attachment_type=video_attachment_type,
        )

    def _detect_media_label(self, permalink_url: str, details: dict[str, Any]) -> str:
        normalized_url = (permalink_url or "").lower()
        if "/reel/" in normalized_url or "/videos/" in normalized_url:
            return "Video"
        status_type = str(details.get("status_type", "")).lower()
        if any(hint in status_type for hint in self._VIDEO_MEDIA_HINTS):
            return "Video"
        attachments = details.get("attachments")
        if self._attachments_contain_video(attachments):
            return "Video"
        return "Anh"

    def _attachments_contain_video(self, node: Any) -> bool:
        if isinstance(node, dict):
            media_type = str(node.get("media_type", "")).lower()
            item_type = str(node.get("type", "")).lower()
            if any(hint in media_type for hint in self._VIDEO_MEDIA_HINTS):
                return True
            if any(hint in item_type for hint in self._VIDEO_MEDIA_HINTS):
                return True
            for value in node.values():
                if self._attachments_contain_video(value):
                    return True
            return False
        if isinstance(node, list):
            return any(self._attachments_contain_video(item) for item in node)
        return False

    def _find_video_attachment(self, node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            media_type = str(node.get("media_type", "")).lower()
            item_type = str(node.get("type", "")).lower()
            if "video" in media_type or "video" in item_type:
                return node
            for value in node.values():
                found = self._find_video_attachment(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = self._find_video_attachment(item)
                if found:
                    return found
        return None

    @staticmethod
    def requires_uploaded_video_post(resolved_post: ResolvedPost) -> bool:
        return (
            str(resolved_post.media_label or "").strip().lower() == "video"
            and str(resolved_post.video_attachment_type or "").strip().lower() == "video_inline"
        )

    def _validate_owner_page(self, owner_page_id: str) -> None:
        if owner_page_id != self.settings.meta_page_id:
            raise ValidationError(
                "Link post không thuộc fanpage đã cấu hình. "
                f"Page của link: {owner_page_id}, page cấu hình: {self.settings.meta_page_id}."
            )

    @staticmethod
    def _first(source: dict[str, list[str]], *keys: str) -> str:
        for key in keys:
            values = source.get(key) or []
            if values and values[0]:
                return str(values[0]).strip()
        return ""

    @staticmethod
    def _extract_pfbid_token(post_url: str) -> str:
        parsed = urlparse(post_url)
        match = re.search(r"/posts/(?P<pfbid>pfbid[\w]+)$", parsed.path or "", re.IGNORECASE)
        if not match:
            return ""
        return match.group("pfbid")

    def _is_permalink_match(self, input_url: str, permalink_url: str, pfbid_token: str) -> bool:
        if not permalink_url:
            return False
        normalized_input = normalize_facebook_url(input_url)
        normalized_permalink = normalize_facebook_url(permalink_url)
        if normalized_input == normalized_permalink:
            return True
        if pfbid_token and pfbid_token.lower() in permalink_url.lower():
            return True
        return False

    def _path_and_params_from_next_url(self, next_url: str) -> tuple[str, dict[str, Any]]:
        from urllib.parse import urlparse as _urlparse
        from urllib.parse import parse_qs as _parse_qs

        parsed = _urlparse(next_url)
        path = re.sub(r"^/v\d+\.\d+", "", parsed.path)
        if not path.startswith("/"):
            path = "/" + path
        params_raw = _parse_qs(parsed.query)
        params: dict[str, Any] = {}
        for key, values in params_raw.items():
            if key == "access_token":
                continue
            if not values:
                continue
            params[key] = values[0]
        return path, params

    def get_saved_audience_targeting(self, saved_audience_id: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/{saved_audience_id}",
            params={"fields": "id,name,targeting"},
        )
        targeting = payload.get("targeting")
        if not isinstance(targeting, dict):
            raise MetaApiError(
                f"Saved audience {saved_audience_id} khong co targeting hop le."
            )
        return targeting

    def find_active_campaigns_by_keywords(self, keywords: list[str]) -> list[dict[str, str]]:
        normalized_keywords: list[str] = []
        seen: set[str] = set()
        for raw_keyword in keywords:
            keyword = str(raw_keyword).strip().upper()
            if not keyword or keyword in seen:
                continue
            seen.add(keyword)
            normalized_keywords.append(keyword)
        if not normalized_keywords:
            return []

        campaigns: list[dict[str, str]] = []
        next_path = f"/{self.ad_account_id}/campaigns"
        next_params: dict[str, Any] | None = {
            "fields": "id,name,effective_status,updated_time",
            "limit": 100,
        }
        scanned = 0

        while next_path and scanned < 5000:
            payload = self._request(
                "GET",
                next_path,
                params=next_params,
            )
            data = payload.get("data", [])
            if not isinstance(data, list):
                data = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                status = str(item.get("effective_status", "")).strip().upper()
                if status != "ACTIVE":
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                normalized_name = name.upper()
                if not all(keyword in normalized_name for keyword in normalized_keywords):
                    continue
                campaigns.append(
                    {
                        "id": str(item.get("id", "")).strip(),
                        "name": name,
                        "effective_status": status,
                        "updated_time": str(item.get("updated_time", "")).strip(),
                    }
                )

            paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
            next_url = str(paging.get("next", "")).strip()
            if not next_url:
                break
            next_path, next_params = self._path_and_params_from_next_url(next_url)

        campaigns.sort(
            key=lambda item: self._parse_meta_datetime(item.get("updated_time", "")),
            reverse=True,
        )
        return campaigns

    def list_eligible_adsets(self, campaign_id: str, max_count: int = 20) -> list[dict[str, str]]:
        normalized_campaign_id = str(campaign_id).strip()
        if not normalized_campaign_id:
            raise ValidationError("Campaign ID trống, không thể lấy danh sách adset.")

        adsets: list[dict[str, str]] = []
        next_path = f"/{normalized_campaign_id}/adsets"
        next_params: dict[str, Any] | None = {
            "fields": "id,name,effective_status,status,updated_time,destination_type",
            "limit": 100,
        }
        scanned = 0

        while next_path and scanned < 5000:
            payload = self._request(
                "GET",
                next_path,
                params=next_params,
            )
            data = payload.get("data", [])
            if not isinstance(data, list):
                data = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                effective_status = str(item.get("effective_status", "")).strip().upper()
                if effective_status not in {"ACTIVE", "PAUSED"}:
                    continue
                adset_id = str(item.get("id", "")).strip()
                adset_name = str(item.get("name", "")).strip()
                if not adset_id:
                    continue
                adsets.append(
                    {
                        "id": adset_id,
                        "name": adset_name,
                        "effective_status": effective_status,
                        "status": str(item.get("status", "")).strip().upper(),
                        "updated_time": str(item.get("updated_time", "")).strip(),
                        "destination_type": str(item.get("destination_type", "")).strip().upper(),
                    }
                )

            paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
            next_url = str(paging.get("next", "")).strip()
            if not next_url:
                break
            next_path, next_params = self._path_and_params_from_next_url(next_url)

        adsets.sort(
            key=lambda item: self._parse_meta_datetime(item.get("updated_time", "")),
            reverse=True,
        )

        safe_limit = max(1, int(max_count))
        if len(adsets) > safe_limit:
            raise ValidationError(
                f"Campaign có {len(adsets)} adset hợp lệ, vượt giới hạn an toàn {safe_limit}. "
                "Anh lọc campaign cụ thể hơn hoặc giảm số adset trước khi chạy."
            )
        return adsets

    def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:
        normalized_adset_id = str(adset_id).strip()
        if not normalized_adset_id:
            raise ValidationError("Thiếu adset_id để lấy cấu hình đa đích.")

        payload = self._request(
            "GET",
            f"/{normalized_adset_id}/ads",
            params={
                "fields": "id,creative{id},updated_time,effective_status,status",
                "limit": max(1, int(max_ads_scan)),
            },
        )
        ads = payload.get("data", [])
        if not isinstance(ads, list):
            ads = []

        creatives: list[dict[str, str]] = []
        for item in ads:
            if not isinstance(item, dict):
                continue
            creative = item.get("creative", {}) if isinstance(item.get("creative"), dict) else {}
            creative_id = str(creative.get("id", "")).strip()
            if not creative_id:
                continue
            creatives.append(
                {
                    "creative_id": creative_id,
                    "updated_time": str(item.get("updated_time", "")).strip(),
                }
            )
        creatives.sort(
            key=lambda row: self._parse_meta_datetime(row.get("updated_time", "")),
            reverse=True,
        )

        for row in creatives:
            creative_id = row["creative_id"]
            creative_payload = self._request(
                "GET",
                f"/{creative_id}",
                params={"fields": "asset_feed_spec"},
            )
            asset_feed_spec = creative_payload.get("asset_feed_spec")
            if self._is_messaging_asset_feed_spec(asset_feed_spec):
                return asset_feed_spec

        raise ValidationError(
            "Không tìm thấy asset_feed_spec từ ads hiện có trong adset đa đích. "
            "Anh tạo trước 1 ads thủ công trong adset này rồi chạy lại giúp em."
        )

    def get_account_multi_destination_asset_feed_spec(self, max_ads_scan: int = 200) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/{self.ad_account_id}/ads",
            params={
                "fields": "id,creative{id},updated_time,effective_status,status",
                "limit": max(1, int(max_ads_scan)),
            },
        )
        ads = payload.get("data", [])
        if not isinstance(ads, list):
            ads = []

        creatives: list[dict[str, str]] = []
        for item in ads:
            if not isinstance(item, dict):
                continue
            creative = item.get("creative", {}) if isinstance(item.get("creative"), dict) else {}
            creative_id = str(creative.get("id", "")).strip()
            if not creative_id:
                continue
            creatives.append(
                {
                    "creative_id": creative_id,
                    "updated_time": str(item.get("updated_time", "")).strip(),
                }
            )

        creatives.sort(
            key=lambda row: self._parse_meta_datetime(row.get("updated_time", "")),
            reverse=True,
        )

        for row in creatives:
            creative_id = row["creative_id"]
            creative_payload = self._request(
                "GET",
                f"/{creative_id}",
                params={"fields": "asset_feed_spec"},
            )
            asset_feed_spec = creative_payload.get("asset_feed_spec")
            if self._is_messaging_asset_feed_spec(asset_feed_spec):
                return asset_feed_spec

        raise ValidationError(
            "Không tìm thấy asset_feed_spec từ ads trong ad account. "
            "Anh tạo trước 1 ads thủ công có nút nhắn tin rồi chạy lại giúp em."
        )

    @staticmethod
    def _is_messaging_asset_feed_spec(asset_feed_spec: Any) -> bool:
        """Reject unrelated asset feeds before attaching one to a Messenger ad."""

        if not isinstance(asset_feed_spec, dict) or not asset_feed_spec:
            return False
        return str(asset_feed_spec.get("optimization_type", "")).strip().upper() == (
            "DOF_MESSAGING_DESTINATION"
        )

    def get_multi_destination_creative_overrides(
            self,
            adset_id: str,
            *,
            max_ads_scan: int = 20,
            expected_call_to_action_type: str | None = None,
        ) -> dict[str, Any]:
            normalized_adset_id = str(adset_id).strip()
            if not normalized_adset_id:
                raise ValidationError("Thiếu adset_id để lấy mẫu creative đa đích.")
            return self._find_multi_destination_creative_overrides(
                f"/{normalized_adset_id}/ads",
                max_ads_scan=max_ads_scan,
                expected_call_to_action_type=expected_call_to_action_type,
                missing_message=(
                    "Không tìm thấy creative mẫu khỏe từ ads hiện có trong adset đa đích. "
                    "Anh tạo trước 1 ads thủ công trong adset này rồi chạy lại giúp em."
                ),
            )

    def get_account_multi_destination_creative_overrides(
            self,
            *,
            max_ads_scan: int = 200,
            expected_call_to_action_type: str | None = None,
        ) -> dict[str, Any]:
            return self._find_multi_destination_creative_overrides(
                f"/{self.ad_account_id}/ads",
                max_ads_scan=max_ads_scan,
                expected_call_to_action_type=expected_call_to_action_type,
                missing_message=(
                    "Không tìm thấy creative mẫu khỏe từ ads trong ad account. "
                    "Anh tạo trước 1 ads thủ công có nút nhắn tin rồi chạy lại giúp em."
                ),
            )

    def get_message_destination_creative_overrides(
            self,
            adset_id: str,
            *,
            max_ads_scan: int = 20,
            expected_call_to_action_type: str | None = None,
        ) -> dict[str, Any]:
            normalized_adset_id = str(adset_id).strip()
            if not normalized_adset_id:
                raise ValidationError("Thiếu adset_id để lấy mẫu creative Messenger.")
            return self._find_multi_destination_creative_overrides(
                f"/{normalized_adset_id}/ads",
                max_ads_scan=max_ads_scan,
                expected_call_to_action_type=expected_call_to_action_type,
                require_asset_feed_spec=False,
                missing_message=(
                    "Không tìm thấy creative mẫu Messenger khỏe từ ads hiện có trong adset. "
                    "Anh tạo trước 1 ads thủ công trong adset này rồi chạy lại giúp em."
                ),
            )

    def get_account_message_destination_creative_overrides(
            self,
            *,
            max_ads_scan: int = 200,
            expected_call_to_action_type: str | None = None,
        ) -> dict[str, Any]:
            return self._find_multi_destination_creative_overrides(
                f"/{self.ad_account_id}/ads",
                max_ads_scan=max_ads_scan,
                expected_call_to_action_type=expected_call_to_action_type,
                require_asset_feed_spec=False,
                missing_message=(
                    "Không tìm thấy creative mẫu Messenger khỏe từ ads trong ad account. "
                    "Anh tạo trước 1 ads thủ công có nút nhắn tin rồi chạy lại giúp em."
                ),
            )

    def _find_multi_destination_creative_overrides(
            self,
            ads_path: str,
            *,
            max_ads_scan: int,
            expected_call_to_action_type: str | None,
            missing_message: str,
            require_asset_feed_spec: bool = True,
        ) -> dict[str, Any]:
            payload = self._request(
                "GET",
                ads_path,
                params={
                    "fields": "id,creative{id},updated_time,effective_status,status",
                    "limit": max(1, int(max_ads_scan)),
                },
            )
            ads = payload.get("data", [])
            if not isinstance(ads, list):
                ads = []

            creatives: list[dict[str, str]] = []
            for item in ads:
                if not isinstance(item, dict):
                    continue
                effective_status = str(item.get("effective_status", "")).strip().upper()
                if effective_status == "WITH_ISSUES":
                    continue
                creative = item.get("creative", {}) if isinstance(item.get("creative"), dict) else {}
                creative_id = str(creative.get("id", "")).strip()
                if not creative_id:
                    continue
                creatives.append(
                    {
                        "creative_id": creative_id,
                        "updated_time": str(item.get("updated_time", "")).strip(),
                    }
                )
            creatives.sort(
                key=lambda row: self._parse_meta_datetime(row.get("updated_time", "")),
                reverse=True,
            )

            for row in creatives:
                creative_id = row["creative_id"]
                creative_payload = self._request(
                    "GET",
                    f"/{creative_id}",
                    params={
                        "fields": (
                            "asset_feed_spec,call_to_action_type,"
                            "degrees_of_freedom_spec,contextual_multi_ads,"
                            "instagram_user_id,instagram_actor_id,page_welcome_message"
                        )
                    },
                )
                expected_cta = str(expected_call_to_action_type or "").strip().upper()
                actual_cta = str(creative_payload.get("call_to_action_type", "")).strip().upper()
                if expected_cta and actual_cta != expected_cta:
                    continue
                asset_feed_spec = creative_payload.get("asset_feed_spec")
                if require_asset_feed_spec and (not isinstance(asset_feed_spec, dict) or not asset_feed_spec):
                    continue
                overrides: dict[str, Any] = {}
                if isinstance(asset_feed_spec, dict) and asset_feed_spec:
                    overrides["asset_feed_spec"] = asset_feed_spec
                if expected_cta:
                    overrides["call_to_action_type"] = actual_cta
                for field in (
                    "degrees_of_freedom_spec",
                    "contextual_multi_ads",
                    "instagram_user_id",
                    "instagram_actor_id",
                    "page_welcome_message",
                ):
                    value = creative_payload.get(field)
                    if value not in (None, "", {}, []):
                        overrides[field] = value
                if overrides:
                    return overrides

            raise ValidationError(missing_message)

    def find_reusable_creative_id_by_story_ids(
        self,
        story_ids: list[str],
        *,
        adset_id: str | None = None,
        max_ads_scan: int = 800,
        expected_page_welcome_message: Any | None = None,
        expected_call_to_action_type: str | None = None,
    ) -> str | None:
        normalized_ids: set[str] = {
            str(item).strip()
            for item in story_ids
            if str(item).strip()
        }
        if not normalized_ids:
            return None
        expected_cta = str(expected_call_to_action_type or "").strip().upper()

        if adset_id and str(adset_id).strip():
            next_path = f"/{str(adset_id).strip()}/ads"
        else:
            next_path = f"/{self.ad_account_id}/ads"
        next_params: dict[str, Any] | None = {
            "fields": (
                "id,updated_time,"
                "creative{"
                "id,object_story_id,effective_object_story_id,"
                "page_welcome_message,call_to_action_type"
                "}"
            ),
            "limit": 200,
        }
        scanned = 0
        candidates: list[dict[str, str]] = []

        while next_path and scanned < max(1, int(max_ads_scan)):
            payload = self._request(
                "GET",
                next_path,
                params=next_params,
            )
            data = payload.get("data", [])
            if not isinstance(data, list):
                data = []

            for item in data:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                creative = item.get("creative")
                if not isinstance(creative, dict):
                    continue
                creative_id = str(creative.get("id", "")).strip()
                if not creative_id:
                    continue
                object_story_id = str(creative.get("object_story_id", "")).strip()
                effective_object_story_id = str(creative.get("effective_object_story_id", "")).strip()
                if object_story_id not in normalized_ids and effective_object_story_id not in normalized_ids:
                    continue
                if not self._page_welcome_message_matches_expected(
                    creative.get("page_welcome_message"),
                    expected_page_welcome_message,
                ):
                    continue
                if expected_cta:
                    actual_cta = str(creative.get("call_to_action_type", "")).strip().upper()
                    if actual_cta != expected_cta:
                        continue
                candidates.append(
                    {
                        "creative_id": creative_id,
                        "updated_time": str(item.get("updated_time", "")).strip(),
                    }
                )

            paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
            next_url = str(paging.get("next", "")).strip()
            if not next_url:
                break
            next_path, next_params = self._path_and_params_from_next_url(next_url)

        if not candidates:
            return None
        candidates.sort(
            key=lambda row: self._parse_meta_datetime(row.get("updated_time", "")),
            reverse=True,
        )
        return candidates[0]["creative_id"]

    def find_latest_ad_by_story_ids(
        self,
        story_ids: list[str],
        *,
        adset_id: str | None = None,
        max_ads_scan: int = 800,
        expected_page_welcome_message: Any | None = None,
        expected_call_to_action_type: str | None = None,
        expected_adset_destination_type: str | None = None,
    ) -> dict[str, str] | None:
        normalized_ids: set[str] = {
            str(item).strip()
            for item in story_ids
            if str(item).strip()
        }
        if not normalized_ids:
            return None
        expected_cta = str(expected_call_to_action_type or "").strip().upper()
        expected_destination = str(expected_adset_destination_type or "").strip().upper()

        if adset_id and str(adset_id).strip():
            next_path = f"/{str(adset_id).strip()}/ads"
        else:
            next_path = f"/{self.ad_account_id}/ads"
        next_params: dict[str, Any] | None = {
            "fields": (
                "id,name,status,effective_status,updated_time,issues_info,"
                "adset{id,destination_type},"
                "creative{"
                "id,object_story_id,effective_object_story_id,"
                "page_welcome_message,call_to_action_type"
                "}"
            ),
            "limit": 200,
        }
        scanned = 0
        candidates: list[dict[str, str]] = []

        while next_path and scanned < max(1, int(max_ads_scan)):
            payload = self._request(
                "GET",
                next_path,
                params=next_params,
            )
            data = payload.get("data", [])
            if not isinstance(data, list):
                data = []

            for item in data:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                effective_status = str(item.get("effective_status", "")).strip().upper()
                if effective_status == "WITH_ISSUES":
                    continue
                if item.get("issues_info"):
                    continue
                adset = item.get("adset")
                if expected_destination and not isinstance(adset, dict):
                    continue
                if expected_destination and isinstance(adset, dict):
                    actual_destination = str(adset.get("destination_type", "")).strip().upper()
                    if actual_destination != expected_destination:
                        continue
                creative = item.get("creative")
                if not isinstance(creative, dict):
                    continue
                object_story_id = str(creative.get("object_story_id", "")).strip()
                effective_object_story_id = str(creative.get("effective_object_story_id", "")).strip()
                if object_story_id not in normalized_ids and effective_object_story_id not in normalized_ids:
                    continue
                if not self._page_welcome_message_matches_expected(
                    creative.get("page_welcome_message"),
                    expected_page_welcome_message,
                ):
                    continue
                if expected_cta:
                    actual_cta = str(creative.get("call_to_action_type", "")).strip().upper()
                    if actual_cta != expected_cta:
                        continue
                candidates.append(
                    {
                        "id": str(item.get("id", "")).strip(),
                        "name": str(item.get("name", "")).strip(),
                        "status": str(item.get("status", "")).strip(),
                        "effective_status": effective_status,
                        "updated_time": str(item.get("updated_time", "")).strip(),
                        "creative_id": str(creative.get("id", "")).strip(),
                        "call_to_action_type": str(creative.get("call_to_action_type", "")).strip(),
                        "object_story_id": object_story_id,
                        "effective_object_story_id": effective_object_story_id,
                        "adset_id": str((adset or {}).get("id", "")).strip()
                        if isinstance(adset, dict)
                        else "",
                        "adset_destination_type": str(
                            (adset or {}).get("destination_type", "")
                        ).strip()
                        if isinstance(adset, dict)
                        else "",
                    }
                )

            paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
            next_url = str(paging.get("next", "")).strip()
            if not next_url:
                break
            next_path, next_params = self._path_and_params_from_next_url(next_url)

        if not candidates:
            return None
        candidates.sort(
            key=lambda row: self._parse_meta_datetime(row.get("updated_time", "")),
            reverse=True,
        )
        return candidates[0]

    def list_adset_ads_for_copy(
            self,
            adset_id: str,
            *,
            max_ads: int = 50,
            include_statuses: list[str] | None = None,
            name_contains: str | None = None,
        ) -> list[dict[str, str]]:
            normalized_adset_id = str(adset_id or "").strip()
            if not normalized_adset_id:
                raise ValidationError("Thiếu adset ID nguồn để lấy ads copy.")

            allowed_statuses = {
                str(item).strip().upper()
                for item in (include_statuses or ["ACTIVE", "PAUSED"])
                if str(item).strip()
            }
            name_filter = str(name_contains or "").strip().upper()
            next_path = f"/{normalized_adset_id}/ads"
            next_params: dict[str, Any] | None = {
                "fields": (
                    "id,name,status,effective_status,updated_time,issues_info,"
                    "creative{"
                    "id,object_story_id,effective_object_story_id,"
                    "page_welcome_message,call_to_action_type"
                    "}"
                ),
                "limit": 100,
            }
            scanned = 0
            ads: list[dict[str, str]] = []

            while next_path and scanned < max(1, int(max_ads)):
                payload = self._request("GET", next_path, params=next_params)
                data = payload.get("data", [])
                if not isinstance(data, list):
                    data = []

                for item in data:
                    if not isinstance(item, dict):
                        continue
                    scanned += 1
                    ad_id = str(item.get("id", "")).strip()
                    ad_name = str(item.get("name", "")).strip()
                    status = str(item.get("status", "")).strip().upper()
                    effective_status = str(item.get("effective_status", "")).strip().upper()
                    if not ad_id:
                        continue
                    if allowed_statuses and status not in allowed_statuses:
                        continue
                    if effective_status == "WITH_ISSUES":
                        continue
                    if item.get("issues_info"):
                        continue
                    if name_filter and name_filter not in ad_name.upper():
                        continue
                    creative = item.get("creative")
                    if not isinstance(creative, dict):
                        creative = {}
                    ads.append(
                        {
                            "id": ad_id,
                            "name": ad_name,
                            "status": status,
                            "effective_status": effective_status,
                            "updated_time": str(item.get("updated_time", "")).strip(),
                            "creative_id": str(creative.get("id", "")).strip(),
                            "call_to_action_type": str(creative.get("call_to_action_type", "")).strip(),
                            "object_story_id": str(creative.get("object_story_id", "")).strip(),
                            "effective_object_story_id": str(
                                creative.get("effective_object_story_id", "")
                            ).strip(),
                        }
                    )
                    if scanned >= max(1, int(max_ads)):
                        break

                paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
                next_url = str(paging.get("next", "")).strip()
                if not next_url or scanned >= max(1, int(max_ads)):
                    break
                next_path, next_params = self._path_and_params_from_next_url(next_url)

            return ads

    def create_campaign(self, plan: PlannedCampaign) -> str:
        payload = {
            "name": plan.campaign_name,
            "objective": plan.objective,
            "status": "PAUSED",
            "buying_type": "AUCTION",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "special_ad_categories": [],
            "daily_budget": plan.budget_daily_vnd,
        }
        payload = self._apply_overrides(
            payload,
            plan.raw.get("campaign_payload_overrides", {}),
        )
        raw_objective = str(payload.get("objective", ""))
        payload["objective"] = self._normalize_campaign_objective(raw_objective)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/campaigns",
            data=payload,
        )
        campaign_id = str(response.get("id", "")).strip()
        if not campaign_id:
            raise MetaApiError("Khong nhan duoc campaign_id sau khi tao campaign.")
        return campaign_id

    def _page_welcome_message_matches_expected(cls, actual: Any, expected: Any | None) -> bool:
        if expected is None:
            return True
        actual_normalized = cls._normalize_page_welcome_message_for_compare(actual)
        expected_normalized = cls._normalize_page_welcome_message_for_compare(expected)
        return cls._contains_expected_page_welcome_message(
            actual_normalized,
            expected_normalized,
        )

    def _normalize_page_welcome_message_for_compare(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped[0] in "[{":
                with suppress(ValueError, TypeError):
                    return cls._normalize_page_welcome_message_for_compare(json.loads(stripped))
            return stripped
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_page_welcome_message_for_compare(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._normalize_page_welcome_message_for_compare(item) for item in value]
        return value

    def _contains_expected_page_welcome_message(cls, actual: Any, expected: Any) -> bool:
            if isinstance(expected, dict):
                if not isinstance(actual, dict):
                    return False
                for key, expected_value in expected.items():
                    if key not in actual:
                        return False
                    if not cls._contains_expected_page_welcome_message(actual[key], expected_value):
                        return False
                return True
            if isinstance(expected, list):
                return isinstance(actual, list) and actual == expected
            return actual == expected

    def create_adset(
        self,
        plan: PlannedCampaign,
        campaign_id: str,
        slot: AudienceSlot,
    ) -> str:
        raw_targeting = plan.raw.get("adset_payload_overrides", {}).get("targeting")
        targeting = (
            raw_targeting
            if isinstance(raw_targeting, dict) and raw_targeting
            else self._get_slot_targeting(slot)
        )
        payload = {
            "name": slot.adset_name,
            "campaign_id": campaign_id,
            "status": "PAUSED",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": self._DEFAULT_MESSAGING_OPTIMIZATION,
            "destination_type": self._DEFAULT_MESSAGING_DESTINATION,
            "targeting": targeting,
            "promoted_object": {
                "page_id": self.settings.meta_page_id,
                "smart_pse_enabled": False,
            },
        }
        payload = self._apply_overrides(
            payload,
            plan.raw.get("adset_payload_overrides", {}),
        )
        payload = self._normalize_adset_payload(payload)
        fallback_payload = self._build_simple_adset_fallback(payload)
        try:
            response = self._request(
                "POST",
                f"/{self.ad_account_id}/adsets",
                data=payload,
            )
        except MetaApiError as exc:
            if fallback_payload == payload:
                raise
            self.logger.warning(
                "Adset %s tao lan dau that bai (%s). Thu fallback payload don gian.",
                slot.adset_name,
                exc,
            )
            response = self._request(
                "POST",
                f"/{self.ad_account_id}/adsets",
                data=fallback_payload,
            )
        adset_id = str(response.get("id", "")).strip()
        if not adset_id:
            raise MetaApiError(f"Khong nhan duoc adset_id sau khi tao adset {slot.label}.")
        return adset_id

    def create_messenger_media_adset(
        self,
        plan: PlannedCampaign,
        campaign_id: str,
        slot: AudienceSlot,
    ) -> str:
        """Create a direct-media Messenger ad set after lifecycle validation."""

        lifecycle_strategy = normalize_web_lifecycle_strategy(
            plan.raw.get("customer_lifecycle_strategy", "")
        )
        if lifecycle_strategy == "NEW_CUSTOMERS_ONLY":
            raise ValidationError(
                "Meta Graph API hiện chưa hỗ trợ công khai trường Customer Lifecycle Strategy "
                "cho camp Messenger media trực tiếp. Bot chưa tạo camp để tránh chạy sai. "
                "Anh chọn 'Tất cả đối tượng' để tạo tự động hoặc thiết lập lựa chọn này trực tiếp trong Ads Manager."
            )
        return self.create_adset(plan, campaign_id, slot)

    def create_web_adset(
        self,
        plan: PlannedCampaign,
        campaign_id: str,
        slot: AudienceSlot,
    ) -> str:
        lifecycle_strategy = normalize_web_lifecycle_strategy(plan.raw.get("customer_lifecycle_strategy", ""))
        if lifecycle_strategy == "NEW_CUSTOMERS_ONLY":
            raise ValidationError(
                "Meta Graph API hiện chưa hỗ trợ công khai trường Customer Lifecycle Strategy "
                "cho cấu trúc camp Web này. Bot chưa tạo camp để tránh chạy sai. "
                "Anh chọn 'Thu hút lượt chuyển đổi từ tất cả đối tượng' để tạo tự động, "
                "hoặc thiết lập chế độ khách hàng mới trực tiếp trong Ads Manager."
            )

        raw_targeting = plan.raw.get("adset_payload_overrides", {}).get("targeting")
        targeting = (
            raw_targeting
            if isinstance(raw_targeting, dict) and raw_targeting
            else self._get_slot_targeting(slot)
        )
        payload = {
            "name": slot.adset_name,
            "campaign_id": campaign_id,
            "status": "PAUSED",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "destination_type": "WEBSITE",
            "targeting": targeting,
            "promoted_object": {
                "pixel_id": self.settings.meta_pixel_id,
                "custom_event_type": "PURCHASE",
            },
        }
        payload = self._apply_overrides(payload, plan.raw.get("adset_payload_overrides", {}))
        # The legacy existing-customer budget cap is not the new Ads Manager
        # lifecycle selector. Do not send a misleading value for all audiences.
        payload.pop("existing_customer_budget_percentage", None)
        payload.pop("customer_lifecycle_strategy", None)
        payload = self._normalize_adset_payload(payload)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/adsets",
            data=payload,
        )
        adset_id = str(response.get("id", "")).strip()
        if not adset_id:
            raise MetaApiError(f"Không nhận được adset_id cho adset Web {slot.label}.")
        return adset_id

    def _get_slot_targeting(self, slot: AudienceSlot) -> dict[str, Any]:
        if isinstance(slot.targeting_override, dict) and slot.targeting_override:
            return slot.targeting_override
        return self.get_saved_audience_targeting(slot.saved_audience_id)

    def create_ad_creative(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        resolved_post: ResolvedPost,
        destination_type_override: str | None = None,
        extra_payload_overrides: dict[str, Any] | None = None,
    ) -> str:
        # Keep the original Page post/Reel story. Rebuilding an inline Reel via
        # video_data creates a new unpublished post and loses social proof.
        destination_type = (
            str(destination_type_override or "").strip().upper()
            or self._effective_destination_type(plan)
        )
        payload: dict[str, Any] = {"name": f"{slot.ad_name}_CR"}
        payload["object_story_id"] = resolved_post.object_story_id
        instagram_user_id = str(getattr(self.settings, "meta_instagram_user_id", "")).strip()
        if instagram_user_id:
            # Reels published/cross-posted by Instagram need the source
            # Instagram identity on the creative. Without it Meta may create
            # the creative but reject the ad as an incompatible post.
            payload["instagram_user_id"] = instagram_user_id
        payload["page_welcome_message"] = plan.message_template_name
        if destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
            payload["degrees_of_freedom_spec"] = self._AUTO_DESTINATION_DOF_SPEC
            payload["contextual_multi_ads"] = {"enroll_status": "OPT_OUT"}
        payload = self._apply_overrides(
            payload,
            plan.raw.get("creative_payload_overrides", {}),
        )
        if extra_payload_overrides:
            payload = deep_merge(payload, dict(extra_payload_overrides))
            if "degrees_of_freedom_spec" in extra_payload_overrides:
                payload["degrees_of_freedom_spec"] = extra_payload_overrides["degrees_of_freedom_spec"]
        # Legacy key can cause unstable creative rendering in Ads Manager UI.
        payload.pop("message_template_name", None)
        payload.pop("page_welcome_message_source_creative_id", None)
        if destination_type == self._DEFAULT_MESSAGING_DESTINATION:
            payload["contextual_multi_ads"] = dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/adcreatives",
            data=payload,
            access_token=self._get_creative_access_token(),
        )
        creative_id = str(response.get("id", "")).strip()
        if not creative_id:
            raise MetaApiError(f"Khong nhan duoc creative_id cho ad {slot.ad_name}.")
        return creative_id

    def create_web_ad_creative(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        media_asset_id: str,
        thumbnail_url: str = "",
        thumbnail_image_hash: str = "",
    ) -> str:
        media_kind = str(plan.media_kind or "image").strip().lower()
        cta = {
            "type": plan.cta_type,
            "value": {"link": plan.destination_url},
        }
        object_story_spec: dict[str, Any] = {"page_id": self.settings.meta_page_id}
        instagram_user_id = str(getattr(self.settings, "meta_instagram_user_id", "")).strip()
        if instagram_user_id:
            object_story_spec["instagram_user_id"] = instagram_user_id
        if media_kind == "video":
            object_story_spec["video_data"] = {
                "video_id": media_asset_id,
                "message": plan.caption,
                "title": plan.headline,
                "call_to_action": cta,
            }
            if str(thumbnail_image_hash or "").strip():
                object_story_spec["video_data"]["image_hash"] = str(thumbnail_image_hash).strip()
            elif str(thumbnail_url or "").strip():
                object_story_spec["video_data"]["image_url"] = str(thumbnail_url).strip()
        else:
            object_story_spec["link_data"] = {
                "image_hash": media_asset_id,
                "link": plan.destination_url,
                "message": plan.caption,
                "name": plan.headline,
                "call_to_action": cta,
            }
        payload = {
            "name": f"{slot.ad_name}_CR",
            "object_story_spec": object_story_spec,
        }
        payload = self._apply_overrides(payload, plan.raw.get("creative_payload_overrides", {}))
        payload["contextual_multi_ads"] = dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/adcreatives",
            data=payload,
        )
        creative_id = str(response.get("id", "")).strip()
        if not creative_id:
            raise MetaApiError(f"Không nhận được creative_id cho Web ad {slot.ad_name}.")
        return creative_id

    def create_messenger_media_ad_creative(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        media_asset_id: str,
    ) -> str:
        """Create an unpublished Messenger creative without a Page post or URL."""

        object_story_spec: dict[str, Any] = {"page_id": self.settings.meta_page_id}
        media_kind = str(plan.media_kind or "image").strip().lower()
        call_to_action = {"type": "MESSAGE_PAGE"}
        if media_kind == "video":
            object_story_spec["video_data"] = {
                "video_id": media_asset_id,
                "message": plan.caption,
                "title": plan.headline,
                "call_to_action": call_to_action,
            }
        else:
            # Meta rejects MESSAGE_PAGE inside photo_data (subcode 1815518).
            # link_data keeps the creative unpublished while giving the
            # Messenger CTA a valid destination without using a user URL.
            messenger_link = f"https://m.me/{self.settings.meta_page_id}"
            object_story_spec["link_data"] = {
                "image_hash": media_asset_id,
                "link": messenger_link,
                "message": plan.caption,
                "name": plan.headline,
                "call_to_action": call_to_action,
            }

        payload: dict[str, Any] = {
            "name": f"{slot.ad_name}_CR",
            "object_story_spec": object_story_spec,
            "page_welcome_message": plan.message_template_name,
            "degrees_of_freedom_spec": self._AUTO_DESTINATION_DOF_SPEC,
        }
        payload = self._apply_overrides(payload, plan.raw.get("creative_payload_overrides", {}))
        payload.pop("message_template_name", None)
        payload.pop("page_welcome_message_source_creative_id", None)
        payload["contextual_multi_ads"] = dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/adcreatives",
            data=payload,
        )
        creative_id = str(response.get("id", "")).strip()
        if not creative_id:
            raise MetaApiError(f"Không nhận được creative_id cho Messenger ad {slot.ad_name}.")
        return creative_id

    def create_ad(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        adset_id: str,
        creative_id: str,
        destination_type_override: str | None = None,
    ) -> str:
        destination_type = (
            str(destination_type_override or "").strip().upper()
            or self._effective_destination_type(plan)
        )
        payload = {
            "name": slot.ad_name,
            "adset_id": adset_id,
            "status": "PAUSED",
            "creative": {"creative_id": creative_id},
        }
        if destination_type == self._DEFAULT_MESSAGING_DESTINATION:
            # Existing adsets with auto destination can require DOF at ad-level as well.
            payload["degrees_of_freedom_spec"] = self._AUTO_DESTINATION_DOF_SPEC
            payload["contextual_multi_ads"] = {"enroll_status": "OPT_OUT"}
        payload = self._apply_overrides(
            payload,
            plan.raw.get("ad_payload_overrides", {}),
        )
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/ads",
            data=payload,
        )
        ad_id = str(response.get("id", "")).strip()
        if not ad_id:
            raise MetaApiError(f"Khong nhan duoc ad_id cho ad {slot.ad_name}.")
        return ad_id

    def create_web_ad(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        adset_id: str,
        creative_id: str,
    ) -> str:
        payload = {
            "name": slot.ad_name,
            "adset_id": adset_id,
            "status": "PAUSED",
            "creative": {"creative_id": creative_id},
        }
        payload = self._apply_overrides(payload, plan.raw.get("ad_payload_overrides", {}))
        payload["contextual_multi_ads"] = dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/ads",
            data=payload,
        )
        ad_id = str(response.get("id", "")).strip()
        if not ad_id:
            raise MetaApiError(f"Không nhận được ad_id cho Web ad {slot.ad_name}.")
        return ad_id

    def create_messenger_media_ad(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        adset_id: str,
        creative_id: str,
    ) -> str:
        payload = {
            "name": slot.ad_name,
            "adset_id": adset_id,
            "status": "PAUSED",
            "creative": {"creative_id": creative_id},
            "degrees_of_freedom_spec": self._AUTO_DESTINATION_DOF_SPEC,
            "contextual_multi_ads": dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT),
        }
        payload = self._apply_overrides(payload, plan.raw.get("ad_payload_overrides", {}))
        payload["contextual_multi_ads"] = dict(self._CONTEXTUAL_MULTI_ADS_OPT_OUT)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/ads",
            data=payload,
        )
        ad_id = str(response.get("id", "")).strip()
        if not ad_id:
            raise MetaApiError(f"Không nhận được ad_id cho Messenger ad {slot.ad_name}.")
        return ad_id

    def update_ad_creative(self, ad_id: str, creative_id: str) -> None:
        """Replace an ad's creative while leaving its campaign/ad set unchanged."""

        normalized_ad_id = str(ad_id or "").strip()
        normalized_creative_id = str(creative_id or "").strip()
        if not normalized_ad_id or not normalized_creative_id:
            raise ValidationError("Thiếu ad_id hoặc creative_id để cập nhật quảng cáo.")
        self._request(
            "POST",
            f"/{normalized_ad_id}",
            data={"creative": {"creative_id": normalized_creative_id}},
        )

    def duplicate_ad_from_source(
        self,
        source_ad_id: str,
        target_ad_name: str | None = None,
        *,
        target_adset_id: str | None = None,
        status_option: str = "PAUSED",
    ) -> str:
        normalized_source_ad_id = str(source_ad_id).strip()
        if not normalized_source_ad_id:
            raise ValidationError("Thieu source ad id de duplicate.")

        normalized_status_option = str(status_option or "").strip().upper() or "PAUSED"
        if normalized_status_option not in {"ACTIVE", "PAUSED", "INHERITED_FROM_SOURCE"}:
            normalized_status_option = "PAUSED"

        copy_payload: dict[str, Any] = {"status_option": normalized_status_option}
        normalized_target_adset_id = str(target_adset_id or "").strip()
        if normalized_target_adset_id:
            copy_payload["adset_id"] = normalized_target_adset_id

        response = self._request(
            "POST",
            f"/{normalized_source_ad_id}/copies",
            data=copy_payload,
        )
        copied_ad_id = self._extract_copied_ad_id(response)
        if not copied_ad_id:
            raise MetaApiError(
                f"Khong nhan duoc ad_id sau khi duplicate source ad {normalized_source_ad_id}."
            )

        desired_name = str(target_ad_name or "").strip()
        if desired_name:
            try:
                self._request("POST", f"/{copied_ad_id}", data={"name": desired_name})
            except MetaApiError as exc:
                self.logger.warning(
                    "Duplicate ad %s tu source %s thanh cong nhung doi ten that bai: %s",
                    copied_ad_id,
                    normalized_source_ad_id,
                    exc,
                )
        return copied_ad_id

    def update_status(self, entity_id: str, status: str) -> None:
        self._request("POST", f"/{entity_id}", data={"status": status})

    def delete_entity(self, entity_id: str) -> None:
        self._request("DELETE", f"/{entity_id}")

    def publish_tree(self, campaign_id: str, adset_ids: list[str], ad_ids: list[str]) -> None:
        self.update_status(campaign_id, "ACTIVE")
        for adset_id in adset_ids:
            self.update_status(adset_id, "ACTIVE")
        for ad_id in ad_ids:
            self.update_status(ad_id, "ACTIVE")

    def publish_ads(self, ad_ids: list[str]) -> None:
        for ad_id in ad_ids:
            self.update_status(ad_id, "ACTIVE")

    def rollback_tree(
        self,
        campaign_id: str | None,
        adset_ids: list[str],
        ad_ids: list[str],
        creative_ids: list[str] | None = None,
    ) -> None:
        for ad_id in reversed(ad_ids):
            self._safe_delete_or_pause(ad_id)
        for creative_id in reversed(creative_ids or []):
            self._safe_delete_or_pause(creative_id)
        for adset_id in reversed(adset_ids):
            self._safe_delete_or_pause(adset_id)
        if campaign_id:
            self._safe_delete_or_pause(campaign_id)

    def _safe_delete_or_pause(self, entity_id: str) -> None:
        try:
            self.delete_entity(entity_id)
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Khong xoa duoc %s: %s. Thu chuyen PAUSED.", entity_id, exc)

        try:
            self.update_status(entity_id, "PAUSED")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Khong the rollback entity %s: %s", entity_id, exc)

    def _apply_overrides(self, base_payload: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        replacements = {
            "META_PAGE_ID": self.settings.meta_page_id,
            "META_AD_ACCOUNT_ID": self.ad_account_id,
            "META_PIXEL_ID": self.settings.meta_pixel_id,
            "META_INSTAGRAM_USER_ID": getattr(self.settings, "meta_instagram_user_id", ""),
        }
        patch = replace_placeholders(overrides, replacements)
        return deep_merge(base_payload, patch)

    @classmethod
    def _normalize_campaign_objective(cls, objective: str) -> str:
        normalized = objective.strip().upper()
        if not normalized:
            raise ValidationError("Campaign objective đang trống trong config/objective.json.")
        return cls._OBJECTIVE_ALIASES.get(normalized, normalized)

    def _normalize_adset_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        if "destination_type" in normalized:
            normalized["destination_type"] = str(normalized["destination_type"]).strip().upper()
        if "billing_event" in normalized:
            normalized["billing_event"] = str(normalized["billing_event"]).strip().upper()
        if "optimization_goal" in normalized:
            normalized["optimization_goal"] = str(normalized["optimization_goal"]).strip().upper()

        raw_result_goal = str(normalized.pop("result_goal", "")).strip().upper()
        if raw_result_goal:
            mapped_optimization = self._LEGACY_RESULT_GOAL_TO_OPTIMIZATION.get(raw_result_goal, "")
            if mapped_optimization:
                normalized["optimization_goal"] = mapped_optimization

        # Keep legacy config key for UI summary only; don't send to adset API payload.
        normalized.pop("conversion_location", None)

        bid_strategy = str(normalized.get("bid_strategy", "")).strip().upper()
        if bid_strategy:
            normalized["bid_strategy"] = bid_strategy
        if bid_strategy == "LOWEST_COST_WITHOUT_CAP":
            for field in self._BID_CONTROL_FIELDS:
                normalized.pop(field, None)
        if not bid_strategy:
            for field in self._BID_CONTROL_FIELDS:
                normalized.pop(field, None)
        return normalized

    def _build_simple_adset_fallback(self, payload: dict[str, Any]) -> dict[str, Any]:
        fallback = dict(payload)
        fallback["billing_event"] = "IMPRESSIONS"
        fallback["optimization_goal"] = self._DEFAULT_MESSAGING_OPTIMIZATION
        fallback["destination_type"] = self._DEFAULT_MESSAGING_DESTINATION
        fallback.pop("result_goal", None)
        fallback.pop("conversion_location", None)
        fallback.pop("bid_strategy", None)
        for field in self._BID_CONTROL_FIELDS:
            fallback.pop(field, None)
        return fallback

    @classmethod
    def is_auto_destination_error(cls, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        return any(marker in message for marker in cls._AUTO_DESTINATION_ERROR_MARKERS)

    @classmethod
    def is_instagram_media_requirement_error(cls, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        return any(marker in message for marker in cls._INSTAGRAM_MEDIA_REQUIREMENT_ERROR_MARKERS)

    @classmethod
    def is_link_ad_cta_locked_error(cls, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        return any(marker in message for marker in cls._LINK_AD_CTA_LOCK_ERROR_MARKERS)

    @classmethod
    def is_post_not_advertisable_error(cls, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        return any(marker in message for marker in cls._POST_NOT_ADVERTISABLE_ERROR_MARKERS)

    @classmethod
    def is_page_ad_access_error(cls, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        return any(marker in message for marker in cls._PAGE_AD_ACCESS_ERROR_MARKERS)

    def effective_destination_type(self, plan: PlannedCampaign) -> str:
        return self._effective_destination_type(plan)

    def _effective_destination_type(self, plan: PlannedCampaign) -> str:
        base = self._DEFAULT_MESSAGING_DESTINATION
        overrides = plan.raw.get("adset_payload_overrides", {})
        if isinstance(overrides, dict):
            override_value = str(overrides.get("destination_type", "")).strip().upper()
            if override_value:
                return override_value
        return base

    @staticmethod
    def _to_vnd_int(value: str) -> int:
        text = str(value).strip().replace(",", "")
        if not text:
            return 0
        try:
            return int(round(float(text)))
        except ValueError:
            return 0

    @staticmethod
    def _page_ad_access_fix_message() -> str:
        return (
            "Token ads hiện chưa đủ quyền quảng cáo cho Trang.\n"
            "Anh mở Meta Business Settings và cấp lại quyền cho System User đang dùng token ads:\n"
            "1) Accounts -> Pages -> chọn đúng page -> Assign people/system users -> bật quyền quảng cáo trang.\n"
            "2) Accounts -> Ad Accounts -> chọn đúng ad account -> gán quyền Advertiser hoặc Admin cho cùng system user.\n"
            "3) Nếu app yêu cầu, thêm quyền pages_read_engagement cho token ads rồi tạo lại token.\n"
            "Xong 3 bước, anh gửi lại link + ngân sách để em tạo camp lại ngay."
        )

    @staticmethod
    def _parse_meta_datetime(value: str) -> datetime:
        text = str(value).strip()
        if not text:
            return datetime.min.replace(tzinfo=timezone.utc)
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _extract_copied_ad_id(payload: dict[str, Any]) -> str:
        for key in ("copied_ad_id", "copied_adgroup_id", "id"):
            value = str(payload.get(key, "")).strip()
            if value:
                return value

        for key in ("copied_ad_ids", "copied_adgroup_ids"):
            values = payload.get(key)
            if isinstance(values, list) and values:
                first = str(values[0]).strip()
                if first:
                    return first

        copies = payload.get("copies")
        if isinstance(copies, list):
            for item in copies:
                if not isinstance(item, dict):
                    continue
                for key in ("id", "copied_ad_id", "copied_adgroup_id", "copy_id"):
                    value = str(item.get(key, "")).strip()
                    if value:
                        return value
        return ""
