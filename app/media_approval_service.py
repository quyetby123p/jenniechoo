from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass
class MediaCallbackAction:
    action: str
    value: str


class MediaApprovalService:
    SHEET_APPLY_PREFIX = "md_sheet_apply:"
    SHEET_CANCEL_PREFIX = "md_sheet_cancel:"
    CLONE_FB_PREFIX = "clone_fb:"
    CLONE_IG_PREFIX = "clone_ig:"
    CLONE_BOTH_PREFIX = "clone_both:"
    CLONE_APPROVE_PREFIX = "clone_approve:"
    CLONE_CANCEL_PREFIX = "clone_cancel:"

    def sheet_sync_keyboard(self, request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Duyệt ghi Google Sheet",
                        callback_data=f"{self.SHEET_APPLY_PREFIX}{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.SHEET_CANCEL_PREFIX}{request_id}",
                    ),
                ]
            ]
        )

    def clone_platform_keyboard(self, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Facebook", callback_data=f"{self.CLONE_FB_PREFIX}{job_id}"),
                    InlineKeyboardButton(text="Instagram", callback_data=f"{self.CLONE_IG_PREFIX}{job_id}"),
                ],
                [
                    InlineKeyboardButton(text="Cả hai", callback_data=f"{self.CLONE_BOTH_PREFIX}{job_id}"),
                    InlineKeyboardButton(text="Hủy", callback_data=f"{self.CLONE_CANCEL_PREFIX}{job_id}"),
                ],
            ]
        )

    def clone_approval_keyboard(self, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Duyệt", callback_data=f"{self.CLONE_APPROVE_PREFIX}{job_id}"),
                    InlineKeyboardButton(text="Hủy", callback_data=f"{self.CLONE_CANCEL_PREFIX}{job_id}"),
                ]
            ]
        )

    def parse_callback(self, callback_data: str | None) -> MediaCallbackAction | None:
        if not callback_data:
            return None
        if callback_data.startswith(self.SHEET_APPLY_PREFIX):
            return MediaCallbackAction(
                action="media_sheet_apply",
                value=callback_data[len(self.SHEET_APPLY_PREFIX):],
            )
        if callback_data.startswith(self.SHEET_CANCEL_PREFIX):
            return MediaCallbackAction(
                action="media_sheet_cancel",
                value=callback_data[len(self.SHEET_CANCEL_PREFIX):],
            )
        clone_callbacks = (
            (self.CLONE_FB_PREFIX, "clone_fb"),
            (self.CLONE_IG_PREFIX, "clone_ig"),
            (self.CLONE_BOTH_PREFIX, "clone_both"),
            (self.CLONE_APPROVE_PREFIX, "clone_approve"),
            (self.CLONE_CANCEL_PREFIX, "clone_cancel"),
        )
        for prefix, action in clone_callbacks:
            if callback_data.startswith(prefix):
                return MediaCallbackAction(action=action, value=callback_data[len(prefix):])
        return None
