from app.media_approval_service import MediaApprovalService


def test_parse_sheet_apply_callback() -> None:
    service = MediaApprovalService()
    action = service.parse_callback("md_sheet_apply:req_123")
    assert action is not None
    assert action.action == "media_sheet_apply"
    assert action.value == "req_123"


def test_parse_sheet_cancel_callback() -> None:
    service = MediaApprovalService()
    action = service.parse_callback("md_sheet_cancel:req_456")
    assert action is not None
    assert action.action == "media_sheet_cancel"
    assert action.value == "req_456"


def test_sheet_sync_keyboard() -> None:
    service = MediaApprovalService()
    keyboard = service.sheet_sync_keyboard("req_1")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["md_sheet_apply:req_1", "md_sheet_cancel:req_1"]
