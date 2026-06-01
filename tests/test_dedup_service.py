from app.dedup_service import DedupService


class FakeStorage:
    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs

    def list_jobs_by_fingerprint(self, post_fingerprint: str) -> list[dict]:
        return [job for job in self.jobs if job.get("post_fingerprint") == post_fingerprint]


def test_dedup_no_existing_job() -> None:
    service = DedupService(storage=FakeStorage([]))
    result = service.inspect("fp-1")
    assert result["is_duplicate"] is False
    assert result["next_version"] == 1


def test_dedup_existing_active_job() -> None:
    jobs = [
        {"job_id": "j1", "post_fingerprint": "fp-1", "version": 1, "status": "pending"},
        {"job_id": "j2", "post_fingerprint": "fp-1", "version": 2, "status": "cancelled"},
    ]
    service = DedupService(storage=FakeStorage(jobs))
    result = service.inspect("fp-1")
    assert result["is_duplicate"] is True
    assert result["next_version"] == 3
    assert len(result["active_jobs"]) == 1
