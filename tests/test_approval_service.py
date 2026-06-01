from app.approval_service import ApprovalService


def test_parse_campaign_pick_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("camp_pick:req_123:2")
    assert action is not None
    assert action.action == "campaign_pick"
    assert action.value == "req_123"
    assert action.index == 2


def test_parse_campaign_cancel_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("camp_cancel:req_123")
    assert action is not None
    assert action.action == "campaign_cancel"
    assert action.value == "req_123"
    assert action.index is None


def test_build_existing_campaign_select_keyboard() -> None:
    service = ApprovalService()
    keyboard = service.existing_campaign_select_keyboard(
        request_id="req_1",
        campaign_options=["1. Camp A", "2. Camp B"],
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["camp_pick:req_1:0", "camp_pick:req_1:1", "camp_cancel:req_1"]


def test_parse_reconcile_apply_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("rc_apply:req_abc")
    assert action is not None
    assert action.action == "reconcile_apply"
    assert action.value == "req_abc"


def test_parse_reconcile_cancel_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("rc_cancel:req_abc")
    assert action is not None
    assert action.action == "reconcile_cancel"
    assert action.value == "req_abc"


def test_parse_reconcile_sheet_apply_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("rc_sheet_apply:req_sheet")
    assert action is not None
    assert action.action == "reconcile_sheet_apply"
    assert action.value == "req_sheet"


def test_parse_reconcile_sheet_cancel_callback() -> None:
    service = ApprovalService()
    action = service.parse_callback("rc_sheet_cancel:req_sheet")
    assert action is not None
    assert action.action == "reconcile_sheet_cancel"
    assert action.value == "req_sheet"
