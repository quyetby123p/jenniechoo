from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from app.assistant_settings import AssistantSettings


class AssistantOpenAIService:
    _RESPONSES_URL = "https://api.openai.com/v1/responses"

    def __init__(self, settings: AssistantSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def is_configured(self) -> tuple[bool, str]:
        if not self.settings.openai_enabled:
            return False, "BOT3_OPENAI_ENABLED=0"
        if not self.settings.openai_api_key:
            return False, "Thiếu BOT3_OPENAI_API_KEY."
        return True, ""

    def ask(self, *, question: str, context_blocks: list[str] | None = None) -> dict[str, Any]:
        ok, reason = self.is_configured()
        if not ok:
            user_message = (
                "Đang tắt OpenAI theo cấu hình BOT3_OPENAI_ENABLED=0, "
                "em sẽ trả lời theo dữ liệu nội bộ."
                if str(reason).strip() == "BOT3_OPENAI_ENABLED=0"
                else reason
            )
            return {
                "ok": False,
                "answer": "",
                "error": reason,
                "error_code": "openai_disabled" if str(reason).strip() == "BOT3_OPENAI_ENABLED=0" else "not_configured",
                "user_message": user_message,
                "warnings": [user_message],
            }

        context_blocks = list(context_blocks or [])
        redacted_question = self.redact_text(question)
        redacted_context = [self.redact_text(block) for block in context_blocks]
        prompt = self._build_prompt(question=redacted_question, contexts=redacted_context)

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Bạn là trợ lý cá nhân tiếng Việt. "
                                "Ưu tiên trả lời ngắn gọn, đúng dữ kiện, chỉ suy luận khi cần và nói rõ khi thiếu dữ liệu."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            ],
            "max_output_tokens": self.settings.openai_max_tokens,
        }

        last_error = ""
        for attempt in range(self.settings.openai_retry_max + 1):
            try:
                response = requests.request(
                    method="POST",
                    url=self._RESPONSES_URL,
                    headers=headers,
                    json=body,
                    timeout=self.settings.openai_timeout_seconds,
                )
                if response.status_code >= 400:
                    text = self._short_text(response.text)
                    classification = self._classify_error_response(response.status_code, response.text)
                    last_error = f"OpenAI API lỗi ({response.status_code}): {text}"
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.settings.openai_retry_max:
                        self._sleep_backoff(attempt)
                        continue
                    return {
                        "ok": False,
                        "answer": "",
                        "error": last_error,
                        "error_code": classification["error_code"],
                        "user_message": classification["user_message"],
                        "warnings": [classification["user_message"]],
                    }
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("OpenAI API trả dữ liệu không hợp lệ.")
                answer = self._extract_output_text(payload)
                if not answer:
                    answer = "Em chưa nhận được nội dung trả lời từ OpenAI."
                return {
                    "ok": True,
                    "answer": answer,
                    "model": str(payload.get("model", self.settings.openai_model)),
                    "warnings": [],
                }
            except Exception as exc:  # noqa: BLE001
                last_error = f"Gọi OpenAI thất bại: {exc}"
                self.logger.warning("Assistant OpenAI call that bai (attempt=%s): %s", attempt + 1, exc)
                if attempt < self.settings.openai_retry_max:
                    self._sleep_backoff(attempt)
                    continue
                break

        return {
            "ok": False,
            "answer": "",
            "error": last_error or "Không thể gọi OpenAI API.",
            "error_code": "runtime_error",
            "user_message": "Em đang không kết nối được OpenAI, anh thử lại sau giúp em.",
            "warnings": [last_error or "Không thể gọi OpenAI API."],
        }

    def redact_text(self, text: str) -> str:
        if not self.settings.redaction_enabled:
            return str(text or "")

        value = str(text or "")
        rules: list[tuple[str, str]] = [
            (r"(?i)\b(EAAG|EAAB|EAAI)[A-Za-z0-9]+", "[REDACTED_TOKEN]"),
            (r"(?i)\b(sk-[A-Za-z0-9_\-]{12,})\b", "[REDACTED_API_KEY]"),
            (r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]"),
            (r"\b(?:\+?84|0)\d{8,10}\b", "[REDACTED_PHONE]"),
            (r"\b\d{12,}\b", "[REDACTED_ID]"),
        ]
        for pattern, replacement in rules:
            value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
        return value

    def _build_prompt(self, *, question: str, contexts: list[str]) -> str:
        lines = ["Câu hỏi:", question.strip()]
        if contexts:
            lines.append("")
            lines.append("Ngữ cảnh nội bộ đã thu thập:")
            for idx, item in enumerate(contexts[:8], start=1):
                snippet = str(item).strip()
                if not snippet:
                    continue
                lines.append(f"{idx}) {snippet[:1200]}")
        lines.append("")
        lines.append("Yêu cầu trả lời bằng tiếng Việt, rõ ràng, thực tế, tránh bịa thông tin.")
        return "\n".join(lines).strip()

    def _sleep_backoff(self, attempt: int) -> None:
        values = self.settings.openai_retry_backoff_seconds
        if not values:
            return
        index = min(attempt, len(values) - 1)
        time.sleep(max(0, int(values[index])))

    def _extract_output_text(self, payload: dict[str, Any]) -> str:
        direct_text = str(payload.get("output_text", "")).strip()
        if direct_text:
            return direct_text

        output = payload.get("output", [])
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text_value = str(block.get("text", "")).strip()
                if text_value:
                    parts.append(text_value)
        return "\n".join(parts).strip()

    @staticmethod
    def _short_text(raw: str, limit: int = 320) -> str:
        text = " ".join(str(raw or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _classify_error_response(status_code: int, raw: str) -> dict[str, str]:
        import json

        error_code = "api_error"
        user_message = "Em đang không gọi được OpenAI, anh thử lại sau giúp em."

        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(str(raw or ""))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:  # noqa: BLE001
            payload = {}

        error_obj = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
        code = str(error_obj.get("code", "")).strip().lower()
        err_type = str(error_obj.get("type", "")).strip().lower()
        message = str(error_obj.get("message", "")).strip().lower()
        hints = " ".join(item for item in (code, err_type, message) if item)

        if "insufficient_quota" in hints or "billing" in hints or "quota" in hints:
            error_code = "insufficient_quota"
            user_message = "OpenAI API key hiện tại đã hết quota/billing, anh nạp thêm quota hoặc đổi key khác giúp em."
        elif "invalid_api_key" in hints or "incorrect api key" in hints:
            error_code = "invalid_api_key"
            user_message = "OpenAI API key chưa đúng hoặc đã bị thu hồi, anh kiểm tra lại BOT3_OPENAI_API_KEY giúp em."
        elif "rate_limit" in hints:
            error_code = "rate_limit"
            user_message = "OpenAI đang giới hạn tần suất, anh đợi 1-2 phút rồi thử lại giúp em."
        elif status_code == 401:
            error_code = "unauthorized"
            user_message = "OpenAI trả về 401 (không xác thực được), anh kiểm tra API key giúp em."
        elif status_code >= 500:
            error_code = "server_error"
            user_message = "OpenAI đang lỗi phía server, anh thử lại sau vài phút giúp em."

        return {"error_code": error_code, "user_message": user_message}
