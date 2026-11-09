from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "free-scheduled-tasks.yml"


def _cache_paths(workflow: str, step_name: str) -> list[str]:
    marker = f"- name: {step_name}"
    step = workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]
    path_block = step.split("          path: |", 1)[1].split("          key:", 1)[0]
    return [line.strip() for line in path_block.splitlines() if line.strip()]


def test_runtime_state_restore_and_save_use_the_same_cache_paths() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert _cache_paths(workflow, "Restore runtime state") == _cache_paths(workflow, "Save runtime state")


def test_runtime_state_cache_includes_ads_job_and_pending_request_state() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    paths = _cache_paths(workflow, "Restore runtime state")

    assert "storage/jobs" in paths
    assert "storage/pending_requests" in paths
