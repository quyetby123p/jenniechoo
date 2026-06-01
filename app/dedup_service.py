from __future__ import annotations

from app.storage_service import StorageService


class DedupService:
    def __init__(self, storage: StorageService) -> None:
        self.storage = storage

    def inspect(self, post_fingerprint: str) -> dict:
        jobs = self.storage.list_jobs_by_fingerprint(post_fingerprint)
        versions = sorted(
            [
                int(job.get("version", 1))
                for job in jobs
                if str(job.get("version", "")).isdigit()
            ]
        )
        max_version = versions[-1] if versions else 0
        next_version = max_version + 1

        active_statuses = {"pending", "published"}
        active_jobs = [job for job in jobs if job.get("status") in active_statuses]

        return {
            "is_duplicate": len(active_jobs) > 0,
            "next_version": next_version if next_version > 0 else 1,
            "existing_jobs": jobs,
            "active_jobs": active_jobs,
        }
