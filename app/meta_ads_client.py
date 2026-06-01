from __future__ import annotations

from datetime import date, datetime, timezone
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


class MetaAdsClient:
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
                message = error.get("error_user_msg") or error.get("message")
                if message:
                    return str(message)
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

    def get_daily_spend(self, report_date: date, timezone_name: str) -> dict[str, Any]:
        del timezone_name  # Meta insights nhận time_range theo ngày; timezone dùng theo ad account.
        time_range = {
            "since": report_date.isoformat(),
            "until": report_date.isoformat(),
        }
        payload = self._request(
            "GET",
            f"/{self.ad_account_id}/insights",
            params={
                "fields": "spend,account_id,date_start,date_stop",
                "level": "account",
                "time_increment": 1,
                "time_range": json.dumps(time_range, ensure_ascii=False),
            },
            access_token=self.settings.meta_access_token,
        )
        data = payload.get("data", [])
        if not isinstance(data, list) or not data:
            raise MetaApiError("Meta không trả dữ liệu spend cho ngày yêu cầu.")

        first = data[0] if isinstance(data[0], dict) else {}
        spend_raw = str(first.get("spend", "0")).strip()
        spend_vnd = self._to_vnd_int(spend_raw)
        return {
            "report_date": report_date.isoformat(),
            "account_id": str(first.get("account_id", self.ad_account_id)),
            "date_start": str(first.get("date_start", report_date.isoformat())),
            "date_stop": str(first.get("date_stop", report_date.isoformat())),
            "spend_vnd": spend_vnd,
            "currency": self.settings.app_currency,
        }

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
            return ResolvedPost(
                post_id=story_fbid,
                page_id=owner_id,
                permalink_url=post_url,
                object_story_id=f"{owner_id}_{story_fbid}",
                strategy="direct_story_fbid",
            )

        path_match = re.search(r"/posts/(?P<post_id>\d+)$", parsed.path or "", re.IGNORECASE)
        if path_match:
            post_id = path_match.group("post_id")
            owner_page_id = self.settings.meta_page_id
            return ResolvedPost(
                post_id=post_id,
                page_id=owner_page_id,
                permalink_url=post_url,
                object_story_id=f"{owner_page_id}_{post_id}",
                strategy="direct_numeric_post",
            )
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
        latest_non_reel: ResolvedPost | None = None

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
                    if latest_non_reel is None and "/posts/" in permalink:
                        latest_non_reel = self._to_resolved_post(
                            post_node_id,
                            permalink or post_url,
                            strategy="fallback_latest_non_reel",
                        )
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

        if latest_non_reel:
            self.logger.warning(
                "Không map được pfbid URL sang permalink. Tạm dùng bài post thường mới nhất: %s",
                latest_non_reel.permalink_url,
            )
            return latest_non_reel

        raise ValidationError(
            "Không tìm thấy bài viết tương ứng trong feed của Trang.\n"
            "Anh thử gửi link permalink đầy đủ hoặc kiểm tra lại quyền Page Access Token."
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
                        "attachments{media_type,type,subattachments{media_type,type}}"
                    )
                },
                access_token=self.settings.meta_page_access_token,
            )
        except Exception:  # noqa: BLE001
            return resolved_post

        message_text = str(details.get("message", "")).strip()
        permalink = str(details.get("permalink_url", "")).strip() or resolved_post.permalink_url
        media_label = self._detect_media_label(permalink, details)
        return ResolvedPost(
            post_id=resolved_post.post_id,
            page_id=resolved_post.page_id,
            permalink_url=permalink,
            object_story_id=resolved_post.object_story_id,
            strategy=resolved_post.strategy,
            message_text=message_text,
            media_label=media_label,
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
            if isinstance(asset_feed_spec, dict) and asset_feed_spec:
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
            if isinstance(asset_feed_spec, dict) and asset_feed_spec:
                return asset_feed_spec

        raise ValidationError(
            "Không tìm thấy asset_feed_spec từ ads trong ad account. "
            "Anh tạo trước 1 ads thủ công có nút nhắn tin rồi chạy lại giúp em."
        )

    def find_reusable_creative_id_by_story_ids(
        self,
        story_ids: list[str],
        *,
        adset_id: str | None = None,
        max_ads_scan: int = 800,
    ) -> str | None:
        normalized_ids: set[str] = {
            str(item).strip()
            for item in story_ids
            if str(item).strip()
        }
        if not normalized_ids:
            return None

        if adset_id and str(adset_id).strip():
            next_path = f"/{str(adset_id).strip()}/ads"
        else:
            next_path = f"/{self.ad_account_id}/ads"
        next_params: dict[str, Any] | None = {
            "fields": "id,updated_time,creative{id,object_story_id,effective_object_story_id}",
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
    ) -> dict[str, str] | None:
        normalized_ids: set[str] = {
            str(item).strip()
            for item in story_ids
            if str(item).strip()
        }
        if not normalized_ids:
            return None

        if adset_id and str(adset_id).strip():
            next_path = f"/{str(adset_id).strip()}/ads"
        else:
            next_path = f"/{self.ad_account_id}/ads"
        next_params: dict[str, Any] | None = {
            "fields": "id,name,status,effective_status,updated_time,creative{id,object_story_id,effective_object_story_id}",
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
                object_story_id = str(creative.get("object_story_id", "")).strip()
                effective_object_story_id = str(creative.get("effective_object_story_id", "")).strip()
                if object_story_id not in normalized_ids and effective_object_story_id not in normalized_ids:
                    continue
                candidates.append(
                    {
                        "id": str(item.get("id", "")).strip(),
                        "name": str(item.get("name", "")).strip(),
                        "status": str(item.get("status", "")).strip(),
                        "effective_status": str(item.get("effective_status", "")).strip(),
                        "updated_time": str(item.get("updated_time", "")).strip(),
                        "creative_id": str(creative.get("id", "")).strip(),
                        "object_story_id": object_story_id,
                        "effective_object_story_id": effective_object_story_id,
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

    def create_adset(
        self,
        plan: PlannedCampaign,
        campaign_id: str,
        slot: AudienceSlot,
    ) -> str:
        targeting = self.get_saved_audience_targeting(slot.saved_audience_id)
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

    def create_ad_creative(
        self,
        plan: PlannedCampaign,
        slot: AudienceSlot,
        resolved_post: ResolvedPost,
        destination_type_override: str | None = None,
        extra_payload_overrides: dict[str, Any] | None = None,
    ) -> str:
        destination_type = (
            str(destination_type_override or "").strip().upper()
            or self._effective_destination_type(plan)
        )
        payload = {
            "name": f"{slot.ad_name}_CR",
            "object_story_id": resolved_post.object_story_id,
            "page_welcome_message": plan.message_template_name,
        }
        if destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
            payload["degrees_of_freedom_spec"] = self._AUTO_DESTINATION_DOF_SPEC
            payload["contextual_multi_ads"] = {"enroll_status": "OPT_OUT"}
        payload = self._apply_overrides(
            payload,
            plan.raw.get("creative_payload_overrides", {}),
        )
        if extra_payload_overrides:
            payload = deep_merge(payload, dict(extra_payload_overrides))
        # Legacy key can cause unstable creative rendering in Ads Manager UI.
        payload.pop("message_template_name", None)
        payload.pop("page_welcome_message_source_creative_id", None)
        response = self._request(
            "POST",
            f"/{self.ad_account_id}/adcreatives",
            data=payload,
        )
        creative_id = str(response.get("id", "")).strip()
        if not creative_id:
            raise MetaApiError(f"Khong nhan duoc creative_id cho ad {slot.ad_name}.")
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
