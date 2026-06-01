from __future__ import annotations

from app.work_progress_api import _normalize_pancake_work_payload, _normalize_zalo_payload


def test_normalize_zalo_payload_with_nested_fields() -> None:
    raw = {
        "event_name": "user_send_text",
        "sender": {"id": "zalo_user_1"},
        "recipient": {"id": "zalo_group_99"},
        "message": {"msg_id": "msg-123", "text": "task: cap nhat #ZL01 doing 40%"},
        "timestamp": 1779945000123,
    }
    normalized = _normalize_zalo_payload(raw)
    assert normalized["event_id"] == "msg-123"
    assert normalized["sender_id"] == "zalo_user_1"
    assert normalized["channel_id"] == "zalo_group_99"
    assert "doing 40%" in normalized["message_text"]
    assert normalized["event_time"] == 1779945000123


def test_normalize_pancake_work_payload_builds_message() -> None:
    raw = {
        "event_id": "pc-1",
        "workspace_id": "ws_11",
        "assignee_id": "emp_99",
        "task": {"title": "Viet report", "status": "doing", "progress": 65},
        "timestamp": 1779945000,
    }
    normalized = _normalize_pancake_work_payload(raw)
    assert normalized["event_id"] == "pc-1"
    assert normalized["channel_id"] == "ws_11"
    assert normalized["sender_id"] == "emp_99"
    assert "Viet report" in normalized["message_text"]
    assert "doing" in normalized["message_text"]


def test_normalize_pancake_work_nested_actor_payload() -> None:
    raw = {
        "id": "evt_7788",
        "data": {
            "workspace_id": "ws_nested_1",
            "actor": {"id": "user_nested_22"},
            "task": {
                "id": "TSK8899",
                "name": "Tong hop KPI",
                "status": "blocked",
                "progress_percent": 55,
            },
            "timestamp": 1779946500123,
        },
    }
    normalized = _normalize_pancake_work_payload(raw)
    assert normalized["event_id"] == "evt_7788"
    assert normalized["channel_id"] == "ws_nested_1"
    assert normalized["sender_id"] == "user_nested_22"
    assert "Tong hop KPI" in normalized["message_text"]
    assert "#TSK8899" in normalized["message_text"]
    assert "blocked" in normalized["message_text"]
