from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import logging
import re
from typing import Any
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from app.media_settings import MediaSettings
from app.utils import now_utc_iso


_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "si",
    "spm",
    "ref",
    "ref_src",
    "mibextid",
}
_VIDEO_HINTS = ("video", "youtube", "tiktok", "reel", "watch", "mp4", "m3u8", "shorts")
_IMAGE_HINTS = ("image", "jpg", "jpeg", "png", "webp", "gif")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "you", "new", "sale",
    "official", "women", "woman", "men", "man", "shop", "store", "online", "best", "top",
    "fashion", "clothing", "wear", "look", "style", "item", "set", "of", "to", "in", "on",
    "ao", "vay", "do", "ngu", "tim", "media", "san", "pham", "anh", "dep", "mau",
    "value", "collection", "currency", "extracted", "secret", "com",
}

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sleepwear": (
        "sleepwear", "nightwear", "nightgown", "nightdress", "pajama", "pyjama", "lingerie",
        "slip dress", "cami", "camisole", "robe", "loungewear", "do ngu", "vay ngu", "ao ngu",
        "satin sleep", "silk sleep", "homewear", "nighty", "nightsuit", "pjs",
    ),
    "dress": (
        "dress", "gown", "maxi", "mini dress", "bodycon", "cocktail dress", "party dress",
        "vay", "dam", "evening dress",
    ),
    "tshirt": (
        "tshirt", "t-shirt", "tee", "shirt", "polo", "ao thun", "ao phong", "oversized tee",
    ),
    "outerwear": (
        "hoodie", "jacket", "coat", "blazer", "cardigan", "sweater", "ao khoac",
    ),
    "pants": (
        "pants", "trousers", "jeans", "shorts", "leggings", "skirt", "quan", "chan vay",
    ),
}

_COLOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "black": ("black", "den", "đen"),
    "white": ("white", "trang", "trắng", "ivory"),
    "green": ("green", "xanh la", "emerald", "olive", "mint", "teal"),
    "blue": ("blue", "xanh duong", "navy", "cobalt", "sky blue"),
    "red": ("red", "do", "đỏ", "burgundy", "maroon"),
    "pink": ("pink", "hong", "hồng", "rose"),
    "beige": ("beige", "kem", "nude", "champagne"),
    "purple": ("purple", "tim", "tím", "lavender"),
    "brown": ("brown", "nau", "nâu", "chocolate"),
    "gray": ("gray", "grey", "xam", "xám", "silver"),
}

_CATEGORY_QUERY_TERMS: dict[str, str] = {
    "sleepwear": "silk sleepwear nightgown",
    "dress": "satin midi dress",
    "tshirt": "fashion tshirt tee",
    "outerwear": "fashion jacket outerwear",
    "pants": "fashion pants skirt",
}

_DESCRIPTOR_TOKENS = {
    "silk",
    "satin",
    "lace",
    "slip",
    "midi",
    "maxi",
    "nightgown",
    "nightwear",
    "lingerie",
    "pajama",
    "sleepwear",
    "green",
    "black",
    "white",
    "red",
    "blue",
    "pink",
    "beige",
}

_SECONDARY_CATEGORY_SIGNALS = {
    "sleepwear",
    "nightgown",
    "nightwear",
    "pajama",
    "lingerie",
    "dress",
    "tshirt",
    "tee",
    "hoodie",
    "jacket",
    "blazer",
    "pants",
    "jeans",
    "skirt",
}


@dataclass(frozen=True)
class _EngineCall:
    engine: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _RelevanceContext:
    anchor_tokens: set[str]
    dominant_category: str
    dominant_color: str
    dominant_category_ratio: float
    strict_mode: bool


class MediaResearchService:
    def __init__(self, settings: MediaSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def run_research(
        self,
        *,
        run_id: str,
        product_code: str,
        keyword_text: str,
        photo_bytes: bytes,
        photo_filename: str,
    ) -> dict[str, Any]:
        created_at = now_utc_iso()
        query_text = self._build_query_text(product_code, keyword_text)
        warnings: list[str] = []
        errors: list[str] = []
        engine_logs: list[dict[str, Any]] = []

        image_url = ""
        try:
            image_url = self._upload_to_cloudinary(photo_bytes, photo_filename)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Upload ảnh Cloudinary lỗi: {exc}")
            return {
                "run_id": run_id,
                "created_at": created_at,
                "product_code": product_code,
                "keyword_text": keyword_text,
                "query_text": query_text,
                "inferred_query": "",
                "market_scope": self.settings.market_scope,
                "input_image_url": image_url,
                "ok": False,
                "partial": False,
                "warnings": warnings,
                "errors": errors,
                "engine_logs": engine_logs,
                "items": [],
            }

        raw_candidates: list[dict[str, Any]] = []
        primary_calls = self._engine_calls_primary(query_text=query_text, image_url=image_url)
        self._execute_engine_calls(
            primary_calls,
            raw_candidates=raw_candidates,
            engine_logs=engine_logs,
            warnings=warnings,
        )

        inferred_query = self._infer_query_from_lens(raw_candidates=raw_candidates, fallback=query_text)
        enable_secondary = self._should_run_secondary_search(
            keyword_text=keyword_text,
            query_text=query_text,
            inferred_query=inferred_query,
            primary_candidate_count=len(raw_candidates),
        )
        if enable_secondary:
            has_user_query = bool(str(keyword_text or "").strip() or str(query_text or "").strip())
            self._execute_engine_calls(
                self._engine_calls_secondary(
                    query_text=inferred_query,
                    include_videos=has_user_query or self._is_strong_inferred_query(inferred_query),
                ),
                raw_candidates=raw_candidates,
                engine_logs=engine_logs,
                warnings=warnings,
            )

        selected_items = self._prepare_candidates(
            raw_candidates=raw_candidates,
            product_code=product_code,
            query_text=query_text,
            relevance_query=inferred_query,
            created_at=created_at,
        )

        image_count = len([item for item in selected_items if item.get("media_type") == "image"])
        video_count = len([item for item in selected_items if item.get("media_type") == "video"])

        ok = bool(selected_items) or not errors
        partial = bool(warnings)
        if not selected_items:
            warnings.append("Không tìm thấy media phù hợp theo bộ lọc hiện tại.")

        return {
            "run_id": run_id,
            "created_at": created_at,
            "product_code": product_code,
            "keyword_text": keyword_text,
            "query_text": query_text,
            "inferred_query": inferred_query,
            "market_scope": self.settings.market_scope,
            "input_image_url": image_url,
            "ok": ok,
            "partial": partial,
            "warnings": warnings,
            "errors": errors,
            "engine_logs": engine_logs,
            "raw_candidate_count": len(raw_candidates),
            "selected_count": len(selected_items),
            "image_count": image_count,
            "video_count": video_count,
            "items": selected_items,
        }

    def _execute_engine_calls(
        self,
        engine_calls: list[_EngineCall],
        *,
        raw_candidates: list[dict[str, Any]],
        engine_logs: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        for engine_call in engine_calls:
            if len(engine_logs) >= self.settings.max_api_calls_per_run:
                break
            try:
                payload = self._call_serpapi(engine_call)
                parsed = self._parse_engine_payload(
                    engine=engine_call.engine,
                    params=engine_call.params,
                    payload=payload,
                )
                raw_candidates.extend(parsed)
                engine_logs.append(
                    {
                        "engine": engine_call.engine,
                        "params": dict(engine_call.params),
                        "status": "ok",
                        "result_count": len(parsed),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                engine_logs.append(
                    {
                        "engine": engine_call.engine,
                        "params": dict(engine_call.params),
                        "status": "error",
                        "error": str(exc),
                    }
                )
                warnings.append(f"Engine {engine_call.engine} lỗi: {exc}")

    def _engine_calls_primary(self, *, query_text: str, image_url: str) -> list[_EngineCall]:
        visual_params: dict[str, Any] = {"url": image_url, "type": "visual_matches", "hl": "en", "gl": "us"}
        products_params: dict[str, Any] = {"url": image_url, "type": "products", "hl": "en", "gl": "us"}
        if query_text:
            visual_params["q"] = query_text
            products_params["q"] = query_text
        return [
            _EngineCall(engine="google_lens", params=visual_params),
            _EngineCall(engine="google_lens", params={"url": image_url, "type": "exact_matches", "hl": "en", "gl": "us"}),
            _EngineCall(engine="google_lens", params=products_params),
        ]

    def _engine_calls_secondary(self, *, query_text: str, include_videos: bool) -> list[_EngineCall]:
        if not query_text:
            return []
        video_query = f"{query_text} product review try on"
        calls = [_EngineCall(engine="google_images", params={"q": query_text, "hl": "en", "gl": "us"})]
        if include_videos:
            calls.extend(
                [
                    _EngineCall(engine="google_videos", params={"q": video_query, "hl": "en", "gl": "us"}),
                    _EngineCall(engine="youtube", params={"search_query": video_query, "hl": "en", "gl": "us"}),
                ]
            )
        return calls

    def _upload_to_cloudinary(self, photo_bytes: bytes, photo_filename: str) -> str:
        cloud_name = str(self.settings.cloudinary_cloud_name).strip()
        upload_preset = str(self.settings.cloudinary_upload_preset).strip().strip('"')
        if not cloud_name or not upload_preset:
            raise RuntimeError("Thiếu MEDIA_RESEARCH_CLOUDINARY_CLOUD_NAME hoặc MEDIA_RESEARCH_CLOUDINARY_UPLOAD_PRESET.")

        url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
        response = requests.request(
            method="POST",
            url=url,
            data={"upload_preset": upload_preset},
            files={"file": (photo_filename or "input.jpg", photo_bytes, "application/octet-stream")},
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloudinary trả lỗi {response.status_code}: {self._short_text(response.text)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Cloudinary trả dữ liệu không hợp lệ.")
        secure_url = str(payload.get("secure_url", "")).strip()
        if not secure_url:
            raise RuntimeError("Cloudinary không trả secure_url.")
        return secure_url

    def _call_serpapi(self, engine_call: _EngineCall) -> dict[str, Any]:
        api_key = str(self.settings.serpapi_api_key).strip()
        if not api_key:
            raise RuntimeError("Thiếu MEDIA_RESEARCH_SERPAPI_API_KEY.")
        params = {
            "api_key": api_key,
            "engine": engine_call.engine,
            **engine_call.params,
        }
        response = requests.request(
            method="GET",
            url="https://serpapi.com/search.json",
            params=params,
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"SerpApi {engine_call.engine} lỗi {response.status_code}: {self._short_text(response.text)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("SerpApi trả dữ liệu không hợp lệ.")
        return payload

    def _parse_engine_payload(self, *, engine: str, params: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if engine == "google_lens":
            return self._parse_google_lens(params=params, payload=payload)
        if engine == "google_images":
            return self._parse_google_images(params=params, payload=payload)
        if engine == "google_videos":
            return self._parse_google_videos(params=params, payload=payload)
        if engine == "youtube":
            return self._parse_youtube(params=params, payload=payload)
        return []

    def _parse_google_lens(self, *, params: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        result_type = str(params.get("type", "visual_matches")).strip().lower()
        key_map = {
            "visual_matches": [("visual_matches", 100.0)],
            "exact_matches": [("exact_matches", 122.0)],
            "products": [("products", 112.0)],
            "all": [("visual_matches", 100.0), ("exact_matches", 122.0), ("products", 112.0)],
        }
        targets = key_map.get(result_type, [("visual_matches", 100.0)])
        parsed: list[dict[str, Any]] = []
        for key, base in targets:
            rows = payload.get(key, [])
            if not isinstance(rows, list):
                continue
            for idx, item in enumerate(rows):
                if not isinstance(item, dict):
                    continue
                source_url = str(item.get("link") or "").strip()
                direct_url = str(item.get("image") or item.get("thumbnail") or "").strip()
                title = str(item.get("title") or "").strip()
                snippet_parts = [
                    str(item.get("source") or "").strip(),
                    str(item.get("snippet") or "").strip(),
                    str(item.get("price") or "").strip(),
                ]
                snippet = " | ".join(part for part in snippet_parts if part)
                parsed.append(
                    {
                        "engine": "google_lens",
                        "lens_type": key,
                        "engine_query": str(params.get("q") or params.get("url") or "").strip(),
                        "source_url": source_url,
                        "direct_media_url": direct_url,
                        "thumbnail_url": str(item.get("thumbnail") or "").strip(),
                        "title": title,
                        "snippet": snippet,
                        "score": float(base - idx),
                    }
                )
        return parsed

    def _parse_google_images(self, *, params: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("images_results", [])
        if not isinstance(rows, list):
            return []
        parsed: list[dict[str, Any]] = []
        for idx, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("link") or item.get("source") or item.get("original") or "").strip()
            direct_url = str(item.get("original") or item.get("thumbnail") or "").strip()
            parsed.append(
                {
                    "engine": "google_images",
                    "engine_query": str(params.get("q", "")).strip(),
                    "source_url": source_url,
                    "direct_media_url": direct_url,
                    "thumbnail_url": str(item.get("thumbnail") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "snippet": str(item.get("snippet") or item.get("source") or "").strip(),
                    "score": float(82 - idx),
                }
            )
        return parsed

    def _parse_google_videos(self, *, params: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("video_results", [])
        if not isinstance(rows, list):
            return []
        parsed: list[dict[str, Any]] = []
        for idx, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("link") or "").strip()
            parsed.append(
                {
                    "engine": "google_videos",
                    "engine_query": str(params.get("q", "")).strip(),
                    "source_url": source_url,
                    "direct_media_url": "",
                    "thumbnail_url": str(item.get("thumbnail") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip(),
                    "score": float(68 - idx),
                }
            )
        return parsed

    def _parse_youtube(self, *, params: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("video_results", [])
        if not isinstance(rows, list):
            return []
        parsed: list[dict[str, Any]] = []
        for idx, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("link") or "").strip()
            thumbnail = item.get("thumbnail", {})
            thumbnail_url = ""
            if isinstance(thumbnail, dict):
                thumbnail_url = str(thumbnail.get("static") or thumbnail.get("rich") or "").strip()
            parsed.append(
                {
                    "engine": "youtube",
                    "engine_query": str(params.get("search_query", "")).strip(),
                    "source_url": source_url,
                    "direct_media_url": "",
                    "thumbnail_url": thumbnail_url,
                    "title": str(item.get("title") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip(),
                    "score": float(64 - idx),
                }
            )
        return parsed

    def _prepare_candidates(
        self,
        *,
        raw_candidates: list[dict[str, Any]],
        product_code: str,
        query_text: str,
        created_at: str,
        relevance_query: str = "",
    ) -> list[dict[str, Any]]:
        context = self._build_relevance_context(raw_candidates=raw_candidates, query_text=relevance_query or query_text)

        deduped: dict[str, dict[str, Any]] = {}
        for item in raw_candidates:
            source_url = self._normalize_url(item.get("source_url"))
            direct_url = self._normalize_url(item.get("direct_media_url"))
            canonical_target = self._canonical_url(direct_url or source_url)
            if not canonical_target:
                continue
            if not self._is_allowlisted(source_url or direct_url):
                continue

            engine = str(item.get("engine", "")).strip().lower()
            media_type = self._detect_media_type(source_url=source_url, direct_url=direct_url, engine=engine)
            if media_type not in {"image", "video"}:
                continue

            status = "ready"
            lowered_url = canonical_target.lower()
            if any(token in lowered_url for token in ("/login", "private", "signin")):
                status = "skipped_private_or_inaccessible"
            if status != "ready":
                continue

            text_blob = self._normalize_for_match(
                " ".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("snippet") or ""),
                        str(source_url or ""),
                    ]
                )
            )
            candidate_tokens = set(self._tokenize(text_blob))
            overlap = len(candidate_tokens.intersection(context.anchor_tokens))
            candidate_categories = self._detect_categories(text_blob)
            candidate_color = self._detect_color(text_blob)

            if context.strict_mode:
                if (
                    context.dominant_category
                    and engine != "google_lens"
                    and candidate_categories
                    and context.dominant_category not in candidate_categories
                ):
                    continue
                if (
                    context.dominant_color
                    and engine != "google_lens"
                    and candidate_color
                    and candidate_color != context.dominant_color
                ):
                    continue
                if context.anchor_tokens and overlap == 0:
                    continue

            score = float(item.get("score") or 0.0)
            if engine == "google_lens":
                score += 25.0
            score += float(overlap * 2)
            if context.dominant_category and candidate_categories:
                if context.dominant_category in candidate_categories:
                    score += 24.0
                else:
                    score -= 58.0 if engine == "google_lens" else 72.0
            elif context.dominant_category and context.strict_mode and engine != "google_lens":
                score -= 14.0
            if context.dominant_color and candidate_color:
                if candidate_color == context.dominant_color:
                    score += 12.0
                else:
                    score -= 24.0

            dedupe_key = f"{product_code}|{media_type}|{canonical_target}"
            current = {
                "created_at": created_at,
                "run_id": "",
                "product_code": product_code,
                "query_text": query_text,
                "market_scope": self.settings.market_scope,
                "media_type": media_type,
                "platform": self._platform_of(source_url or direct_url),
                "title": str(item.get("title") or "").strip(),
                "source_url": source_url,
                "direct_media_url": direct_url,
                "thumbnail_url": self._normalize_url(item.get("thumbnail_url")),
                "snippet": str(item.get("snippet") or "").strip(),
                "engine": str(item.get("engine") or "").strip(),
                "engine_query": str(item.get("engine_query") or "").strip(),
                "score": round(score, 2),
                "status": status,
                "dedupe_key": dedupe_key,
            }
            previous = deduped.get(dedupe_key)
            if not previous or float(current.get("score", 0.0)) > float(previous.get("score", 0.0)):
                deduped[dedupe_key] = current

        items = list(deduped.values())
        items.sort(key=lambda row: float(row.get("score", 0.0)), reverse=True)

        images = [row for row in items if row.get("media_type") == "image"][: self.settings.max_image_results]
        videos = [row for row in items if row.get("media_type") == "video"][: self.settings.max_video_results]
        return images + videos

    def _build_query_text(self, product_code: str, keyword_text: str) -> str:
        keyword = str(keyword_text or "").strip()
        if keyword:
            return keyword

        code = str(product_code or "").strip()
        if not code:
            return ""
        if code.upper().startswith("AUTO"):
            return ""
        if self._looks_like_internal_code(code):
            return ""
        return code

    def _infer_query_from_lens(self, *, raw_candidates: list[dict[str, Any]], fallback: str) -> str:
        lens_rows = [row for row in raw_candidates if str(row.get("engine", "")).strip().lower() == "google_lens"]
        if not lens_rows:
            return str(fallback or "").strip()

        context = self._build_relevance_context(raw_candidates=lens_rows, query_text=fallback)
        token_counter: Counter[str] = Counter()
        for row in lens_rows[:20]:
            title = str(row.get("title") or "")
            snippet = str(row.get("snippet") or "")
            text = self._normalize_for_match(f"{title} {snippet}")
            token_counter.update(self._tokenize(text))

        descriptive_tokens = [
            token
            for token, count in token_counter.most_common(12)
            if count >= 2 and token in _DESCRIPTOR_TOKENS
        ][:4]

        query_parts: list[str] = []
        category_term = _CATEGORY_QUERY_TERMS.get(context.dominant_category, "")
        if category_term:
            query_parts.append(category_term)
        if context.dominant_color:
            query_parts.append(context.dominant_color)
        query_parts.extend(descriptive_tokens)

        if not query_parts:
            top_tokens = [token for token, count in token_counter.most_common(5) if count >= 2]
            query_parts.extend(top_tokens)

        # Keep stable order, drop duplicates.
        inferred = " ".join(dict.fromkeys(part.strip() for part in query_parts if part.strip()))
        if inferred:
            return inferred
        return str(fallback or "").strip()

    def _build_relevance_context(self, *, raw_candidates: list[dict[str, Any]], query_text: str) -> _RelevanceContext:
        lens_rows = [row for row in raw_candidates if str(row.get("engine", "")).strip().lower() == "google_lens"]
        seed_rows = lens_rows if lens_rows else raw_candidates

        token_counter: Counter[str] = Counter()
        category_counter: Counter[str] = Counter()
        color_counter: Counter[str] = Counter()

        for row in seed_rows[:40]:
            title = str(row.get("title") or "")
            snippet = str(row.get("snippet") or "")
            text = self._normalize_for_match(f"{title} {snippet}")
            tokens = self._tokenize(text)
            token_counter.update(tokens)

            weight = self._row_weight(row)
            for category in self._detect_categories(text):
                category_counter[category] += weight

            color = self._detect_color(text)
            if color:
                color_counter[color] += weight

        if query_text:
            query_tokens = self._tokenize(self._normalize_for_match(query_text))
            token_counter.update(query_tokens)

        anchor_tokens = {token for token, count in token_counter.most_common(20) if count >= 2}
        if not anchor_tokens and query_text:
            anchor_tokens = set(self._tokenize(self._normalize_for_match(query_text)))

        dominant_category = self._select_dominant_category(category_counter)
        dominant_color = color_counter.most_common(1)[0][0] if color_counter else ""
        dominant_score = float(category_counter.get(dominant_category, 0)) if dominant_category else 0.0
        total_category_score = float(sum(category_counter.values()))
        dominant_category_ratio = (dominant_score / total_category_score) if total_category_score > 0 else 0.0
        strict_mode = len(lens_rows) >= 3 and bool(dominant_category) and dominant_score >= 3.0
        return _RelevanceContext(
            anchor_tokens=anchor_tokens,
            dominant_category=dominant_category,
            dominant_color=dominant_color,
            dominant_category_ratio=dominant_category_ratio,
            strict_mode=strict_mode,
        )

    def _is_allowlisted(self, url: str) -> bool:
        host = self._extract_host(url)
        if not host:
            return False
        for allow in self.settings.platform_allowlist:
            allow_norm = str(allow or "").strip().lower()
            if not allow_norm:
                continue
            if host == allow_norm or host.endswith("." + allow_norm):
                return True
        return False

    @staticmethod
    def _extract_host(url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""
        return str(parsed.netloc or "").lower().strip()

    def _platform_of(self, url: str) -> str:
        host = self._extract_host(url)
        if not host:
            return "unknown"
        mapping = (
            "tiktok.com",
            "instagram.com",
            "facebook.com",
            "pinterest.com",
            "shopee.vn",
            "lazada.vn",
            "tokopedia.com",
            "aliexpress.com",
            "amazon.com",
            "etsy.com",
            "youtube.com",
            "youtu.be",
        )
        for domain in mapping:
            if host == domain or host.endswith("." + domain):
                return domain
        return host

    def _detect_media_type(self, *, source_url: str, direct_url: str, engine: str) -> str:
        sample = " ".join([source_url.lower(), direct_url.lower(), engine.lower()])
        if any(hint in sample for hint in _VIDEO_HINTS):
            return "video"
        if any(hint in sample for hint in _IMAGE_HINTS):
            return "image"
        if engine.lower() in {"youtube", "google_videos"}:
            return "video"
        return "image"

    def _normalize_url(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.startswith("//"):
            raw = "https:" + raw
        if not raw.startswith("http://") and not raw.startswith("https://"):
            return ""
        return raw

    def _canonical_url(self, value: str) -> str:
        raw = self._normalize_url(value)
        if not raw:
            return ""
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""

        query_pairs: list[tuple[str, str]] = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=False):
            lowered = key.lower()
            if lowered in _TRACKING_QUERY_KEYS:
                continue
            if any(lowered.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
                continue
            query_pairs.append((key, val))
        query_pairs.sort(key=lambda item: (item[0], item[1]))

        normalized = parsed._replace(
            scheme=parsed.scheme.lower() or "https",
            netloc=(parsed.netloc or "").lower(),
            query=urlencode(query_pairs, doseq=True),
            fragment="",
        )
        return urlunparse(normalized)

    def _should_run_secondary_search(
        self,
        *,
        keyword_text: str,
        query_text: str,
        inferred_query: str,
        primary_candidate_count: int,
    ) -> bool:
        if not inferred_query:
            return False
        has_user_query = bool(str(keyword_text or "").strip() or str(query_text or "").strip())
        if has_user_query:
            return True
        # Image-only mode: avoid drifting results from free-text search when Lens already has enough matches.
        return primary_candidate_count < 8 or self._is_strong_inferred_query(inferred_query)

    def _is_strong_inferred_query(self, query_text: str) -> bool:
        normalized = self._normalize_for_match(query_text)
        if not normalized:
            return False
        tokens = self._tokenize(normalized)
        if len(tokens) < 3:
            return False
        return any(token in _SECONDARY_CATEGORY_SIGNALS for token in tokens)

    @staticmethod
    def _row_weight(row: dict[str, Any]) -> int:
        lens_type = str(row.get("lens_type", "")).strip().lower()
        if lens_type == "exact_matches":
            return 3
        if lens_type == "products":
            return 2
        return 1

    def _select_dominant_category(self, category_counter: Counter[str]) -> str:
        if not category_counter:
            return ""
        top_category, top_score = category_counter.most_common(1)[0]
        sleepwear_score = category_counter.get("sleepwear", 0)
        dress_score = category_counter.get("dress", 0)
        # Sleepwear terms are more specific than "dress"; if both appear with meaningful ratio, prefer sleepwear.
        if top_category == "dress" and sleepwear_score >= 8 and dress_score > 0:
            if float(sleepwear_score) / float(dress_score) >= 0.35:
                return "sleepwear"
        return top_category

    def _detect_categories(self, normalized_text: str) -> set[str]:
        if not normalized_text:
            return set()
        categories: set[str] = set()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if self._contains_phrase(normalized_text, keyword):
                    categories.add(category)
                    break
        return categories

    def _detect_category(self, normalized_text: str) -> str:
        categories = self._detect_categories(normalized_text)
        if not categories:
            return ""
        preferred_order = ("sleepwear", "dress", "tshirt", "outerwear", "pants")
        for category in preferred_order:
            if category in categories:
                return category
        return next(iter(categories))

    def _detect_color(self, normalized_text: str) -> str:
        if not normalized_text:
            return ""
        for color, keywords in _COLOR_KEYWORDS.items():
            for keyword in keywords:
                if self._contains_phrase(normalized_text, keyword):
                    return color
        return ""

    def _tokenize(self, normalized_text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", normalized_text)
        return [token for token in tokens if len(token) >= 3 and token not in _STOPWORDS]

    def _normalize_for_match(self, text: str) -> str:
        folded = unicodedata.normalize("NFD", str(text or ""))
        no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
        lowered = no_accents.lower().replace("đ", "d")
        return re.sub(r"\s+", " ", lowered).strip()

    def _contains_phrase(self, normalized_text: str, keyword: str) -> bool:
        phrase = self._normalize_for_match(keyword)
        if not phrase:
            return False
        if " " in phrase:
            return phrase in normalized_text
        return bool(re.search(rf"\b{re.escape(phrase)}\b", normalized_text))

    @staticmethod
    def _looks_like_internal_code(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        upper = raw.upper()
        if upper.startswith("JC") or upper.startswith("SKU"):
            return True
        has_digits = any(ch.isdigit() for ch in raw)
        has_letters = any(ch.isalpha() for ch in raw)
        return has_digits and has_letters and len(raw) <= 12

    @staticmethod
    def _short_text(raw: str, limit: int = 360) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
