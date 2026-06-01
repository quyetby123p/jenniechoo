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
        return None
