from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any

from app.assistant_models import AssistantMemoryHit
from app.assistant_settings import AssistantSettings


class AssistantMemoryService:
    def __init__(self, settings: AssistantSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.db_path = settings.memory_index_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def rebuild_index(self) -> dict[str, Any]:
        candidates = self._collect_candidate_files()
        inserted = 0
        updated = 0
        skipped = 0

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for path in candidates:
                try:
                    result = self._upsert_file(conn, path)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("Index memory file that bai: %s | %s", path, exc)
                    skipped += 1
                    continue
                if result == "inserted":
                    inserted += 1
                elif result == "updated":
                    updated += 1
                else:
                    skipped += 1

        return {
            "ok": True,
            "files_total": len(candidates),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "db_path": str(self.db_path),
        }

    def search(self, query: str, *, limit: int = 6) -> list[AssistantMemoryHit]:
        text = str(query or "").strip()
        if not text:
            return []
        tokens = _query_tokens(text)
        if not tokens:
            return []

        where_clause, params = _build_like_where(tokens, max_tokens=6)
        sql = (
            "SELECT source, path, content, timestamp, mtime "
            "FROM memory_docs "
            f"WHERE {where_clause} "
            "ORDER BY mtime DESC "
            "LIMIT 200"
        )
        rows: list[sqlite3.Row] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        hits: list[tuple[float, AssistantMemoryHit]] = []
        now_ts = datetime.now(timezone.utc).timestamp()
        for row in rows:
            content = str(row["content"] or "")
            score, excerpt = _score_and_excerpt(content, tokens=tokens)
            if score <= 0:
                continue
            mtime = _to_float(row["mtime"], fallback=0.0)
            recency_hours = max(0.0, (now_ts - mtime) / 3600.0) if mtime > 0 else 99999.0
            recency_bonus = max(0.0, 1.5 - min(1.5, recency_hours / 240.0))
            final_score = round(score + recency_bonus, 4)
            hit = AssistantMemoryHit(
                source=str(row["source"] or ""),
                path=str(row["path"] or ""),
                excerpt=excerpt,
                score=final_score,
                timestamp=str(row["timestamp"] or ""),
            )
            hits.append((final_score, hit))

        hits.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in hits[: max(1, limit)]]

    def get_status(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(1) FROM memory_docs").fetchone()
        return {
            "db_path": str(self.db_path),
            "doc_count": int(total[0]) if total else 0,
        }

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_docs (
                    path TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_docs_source ON memory_docs(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_docs_mtime ON memory_docs(mtime DESC)")
            conn.commit()

    def _collect_candidate_files(self) -> list[Path]:
        files: list[Path] = []

        memory_root = self.settings.memory_root
        if memory_root.exists():
            for pattern in ("*.md", "*.json", "*.txt"):
                files.extend(memory_root.rglob(pattern))

        project_storage = self.settings.project_root / "storage"
        if project_storage.exists():
            for pattern in ("*.json", "*.md", "*.csv"):
                files.extend(project_storage.rglob(pattern))

        project_logs = self.settings.project_root / "logs"
        if project_logs.exists():
            for pattern in ("*.log",):
                files.extend(project_logs.rglob(pattern))

        dedup: dict[str, Path] = {}
        for path in files:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            if resolved.stat().st_size > 800_000:
                continue
            dedup[str(resolved)] = resolved
        return sorted(dedup.values(), key=lambda item: str(item))

    def _upsert_file(self, conn: sqlite3.Connection, path: Path) -> str:
        stat = path.stat()
        mtime = float(stat.st_mtime)
        size = int(stat.st_size)
        path_key = str(path.resolve())

        row = conn.execute("SELECT mtime, size FROM memory_docs WHERE path = ?", (path_key,)).fetchone()
        if row is not None:
            old_mtime = _to_float(row[0], fallback=0.0)
            old_size = _to_int(row[1], fallback=-1)
            if abs(old_mtime - mtime) < 1e-6 and old_size == size:
                return "skipped"

        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return "skipped"

        source = self._infer_source(path)
        timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        indexed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO memory_docs(path, source, content, timestamp, mtime, size, indexed_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                source=excluded.source,
                content=excluded.content,
                timestamp=excluded.timestamp,
                mtime=excluded.mtime,
                size=excluded.size,
                indexed_at=excluded.indexed_at
            """,
            (path_key, source, content, timestamp, mtime, size, indexed_at),
        )
        conn.commit()
        return "updated" if row is not None else "inserted"

    def _infer_source(self, path: Path) -> str:
        raw = str(path).lower()
        if "\\memory\\" in raw or "/memory/" in raw:
            return "workspace_memory"
        if "\\storage\\reports\\" in raw or "/storage/reports/" in raw:
            return "internal_report"
        if "\\storage\\reconcile_cod\\" in raw or "/storage/reconcile_cod/" in raw:
            return "cod_reconcile"
        if "\\storage\\media_research\\" in raw or "/storage/media_research/" in raw:
            return "media_research"
        if "\\logs\\" in raw or "/logs/" in raw:
            return "runtime_log"
        return "workspace_file"


def _query_tokens(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    tokens = [token.strip() for token in cleaned.split() if len(token.strip()) >= 2]
    dedup: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        dedup.append(token)
    return dedup


def _build_like_where(tokens: list[str], *, max_tokens: int) -> tuple[str, list[str]]:
    chosen = tokens[: max(1, max_tokens)]
    clauses = ["content LIKE ?" for _ in chosen]
    params = [f"%{token}%" for token in chosen]
    return "(" + " OR ".join(clauses) + ")", params


def _score_and_excerpt(content: str, *, tokens: list[str]) -> tuple[float, str]:
    lowered = content.lower()
    score = 0.0
    first_idx = -1
    for token in tokens:
        count = lowered.count(token.lower())
        if count <= 0:
            continue
        if first_idx < 0:
            first_idx = lowered.find(token.lower())
        score += min(3.0, float(count))
    if score <= 0:
        return 0.0, ""
    if first_idx < 0:
        first_idx = 0
    start = max(0, first_idx - 120)
    end = min(len(content), first_idx + 260)
    excerpt = content[start:end].replace("\r", " ").replace("\n", " ").strip()
    return score, excerpt[:320]


def _to_float(value: Any, *, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _to_int(value: Any, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
