"""SQLite-backed run state, checkpoints, rejected records, and exact dedupe."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ._lock import acquire_exclusive, release_exclusive  # 跨平台文件锁（fcntl/msvcrt）
from .errors import ResumeMismatchError, RunLockedError
from .models import RunStatus
from .utils import utc_now


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.CREATED.value: {RunStatus.PLANNING.value, RunStatus.FAILED.value},
    RunStatus.PLANNING.value: {
        RunStatus.ACQUIRING.value,
        RunStatus.WAITING_FOR_INPUT.value,
        RunStatus.FAILED.value,
    },
    RunStatus.ACQUIRING.value: {
        RunStatus.NORMALIZING.value,
        RunStatus.WAITING_FOR_INPUT.value,
        RunStatus.FAILED.value,
    },
    RunStatus.NORMALIZING.value: {
        RunStatus.ACQUIRING.value,
        RunStatus.DEDUPING.value,
        RunStatus.FAILED.value,
    },
    RunStatus.DEDUPING.value: {
        RunStatus.ACQUIRING.value,
        RunStatus.VALIDATING.value,
        RunStatus.FAILED.value,
    },
    RunStatus.VALIDATING.value: {
        RunStatus.READY_TO_COMMIT.value,
        RunStatus.COMPLETED_WITH_GAPS.value,
        RunStatus.SUCCEEDED_DRY_RUN.value,
        RunStatus.ACQUIRING.value,
        RunStatus.FAILED.value,
    },
    RunStatus.READY_TO_COMMIT.value: {
        RunStatus.ACQUIRING.value,
        RunStatus.ACTIVATING.value,
        RunStatus.FAILED.value,
    },
    RunStatus.ACTIVATING.value: {
        RunStatus.ACQUIRING.value,
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.WAITING_FOR_INPUT.value: {RunStatus.ACQUIRING.value, RunStatus.FAILED.value},
    RunStatus.COMPLETED_WITH_GAPS.value: {RunStatus.ACQUIRING.value, RunStatus.FAILED.value},
    RunStatus.SUCCEEDED_DRY_RUN.value: {RunStatus.ACQUIRING.value, RunStatus.FAILED.value},
}


@dataclass(frozen=True)
class AddResult:
    disposition: str
    document_id: str
    added_assignment: bool
    reason: str = ""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def run_lock(self, run_id: str):
        """Hold an OS-level exclusive lease for one run; released on process exit."""

        lock_dir = self.path.parent / ".collection_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{self.path.name}.{run_id}.lock"
        stream = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                acquire_exclusive(stream)
            except BlockingIOError as exc:
                raise RunLockedError(
                    f"run {run_id} 正在被另一个采集进程使用；请等待它结束后再续跑"
                ) from exc
            yield
        finally:
            try:
                release_exclusive(stream)
            finally:
                stream.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                status TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                catalog_fingerprint TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                min_categories INTEGER NOT NULL,
                min_documents INTEGER NOT NULL,
                min_per_category INTEGER NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                publication_base_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                status TEXT NOT NULL,
                cursor TEXT,
                seen INTEGER NOT NULL DEFAULT 0,
                accepted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                conflicts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, category_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS records (
                run_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, document_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS dedupe_keys (
                run_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                document_id TEXT NOT NULL,
                PRIMARY KEY (run_id, dedupe_key),
                FOREIGN KEY (run_id, document_id) REFERENCES records(run_id, document_id)
            );
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                raw_locator TEXT NOT NULL,
                title TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            """
        )
        run_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "publication_base_json" not in run_columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN publication_base_json TEXT NOT NULL DEFAULT ''"
            )
        self.connection.commit()

    def create_run(
        self,
        *,
        run_id: str,
        source_name: str,
        config_fingerprint: str,
        catalog_fingerprint: str,
        adapter_version: str,
        min_categories: int,
        min_documents: int,
        min_per_category: int,
    ) -> None:
        existing = self.connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing is not None:
            raise ResumeMismatchError(f"run 已存在: {run_id}；继续它请使用 --resume")
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO runs (
                    run_id, source_name, status, config_fingerprint, catalog_fingerprint,
                    adapter_version, created_at, updated_at, min_categories,
                    min_documents, min_per_category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    source_name,
                    RunStatus.CREATED.value,
                    config_fingerprint,
                    catalog_fingerprint,
                    adapter_version,
                    now,
                    now,
                    min_categories,
                    min_documents,
                    min_per_category,
                ),
            )
            self._event(run_id, RunStatus.CREATED.value, "run_created", {})

    def require_resume_match(
        self,
        run_id: str,
        *,
        source_name: str,
        config_fingerprint: str,
        catalog_fingerprint: str,
        adapter_version: str,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        expected = (source_name, config_fingerprint, catalog_fingerprint, adapter_version)
        actual = (
            run["source_name"],
            run["config_fingerprint"],
            run["catalog_fingerprint"],
            run["adapter_version"],
        )
        if actual != expected:
            raise ResumeMismatchError("续跑失败：数据源、配置、类别表或适配器版本已变化，请新建 run")
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ResumeMismatchError(f"找不到 run: {run_id}")
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
        base_json = result.pop("publication_base_json", "")
        result["publication_base_captured"] = bool(base_json)
        result["publication_base_sha256"] = json.loads(base_json) if base_json else None
        return result

    def capture_publication_base(self, run_id: str, base_sha256: Optional[str]) -> Optional[str]:
        """Persist the first current-pointer snapshot; later calls cannot rebase it."""

        encoded = json.dumps(base_sha256)
        with self.connection:
            self.connection.execute(
                """UPDATE runs SET publication_base_json = ?, updated_at = ?
                   WHERE run_id = ? AND publication_base_json = ''""",
                (encoded, utc_now(), run_id),
            )
        run = self.get_run(run_id)
        return run["publication_base_sha256"]

    def transition(self, run_id: str, new_status: RunStatus, message: str, details: dict[str, Any] | None = None) -> None:
        run = self.get_run(run_id)
        old_status = run["status"]
        if old_status == new_status.value:
            return
        allowed = ALLOWED_TRANSITIONS.get(old_status, set())
        if new_status.value not in allowed:
            raise RuntimeError(f"非法状态迁移: {old_status} -> {new_status.value}")
        completed_at = utc_now() if new_status in {RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_DRY_RUN, RunStatus.FAILED} else None
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
                (new_status.value, utc_now(), completed_at, run_id),
            )
            self._event(run_id, new_status.value, message, details or {})

    def fail(self, run_id: str, error: str) -> None:
        run = self.get_run(run_id)
        if run["status"] in {
            RunStatus.FAILED.value,
            RunStatus.SUCCEEDED.value,
            RunStatus.SUCCEEDED_DRY_RUN.value,
        }:
            return
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
                (RunStatus.FAILED.value, error, utc_now(), utc_now(), run_id),
            )
            self._event(run_id, RunStatus.FAILED.value, "run_failed", {"error": error})

    def save_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET metrics_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(metrics, ensure_ascii=False, sort_keys=True), utc_now(), run_id),
            )

    def ensure_task(self, run_id: str, category_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND category_id = ?",
            (run_id, category_id),
        ).fetchone()
        if row is None:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO tasks (run_id, category_id, status, updated_at) VALUES (?, ?, 'PENDING', ?)",
                    (run_id, category_id, utc_now()),
                )
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? AND category_id = ?",
                (run_id, category_id),
            ).fetchone()
        return dict(row)

    def checkpoint_task(
        self,
        run_id: str,
        category_id: str,
        *,
        status: str,
        cursor: Optional[str],
        seen: int,
        accepted: int,
        rejected: int,
        duplicates: int,
        conflicts: int,
        error: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET status = ?, cursor = ?, seen = ?, accepted = ?, rejected = ?,
                    duplicates = ?, conflicts = ?, error = ?, updated_at = ?
                    WHERE run_id = ? AND category_id = ?""",
                (
                    status,
                    cursor,
                    seen,
                    accepted,
                    rejected,
                    duplicates,
                    conflicts,
                    error,
                    utc_now(),
                    run_id,
                    category_id,
                ),
            )

    def add_document(self, run_id: str, document: dict[str, Any], keys: list[str]) -> AddResult:
        placeholders = ",".join("?" for _ in keys)
        rows = self.connection.execute(
            f"SELECT DISTINCT document_id FROM dedupe_keys WHERE run_id = ? AND dedupe_key IN ({placeholders})",
            (run_id, *keys),
        ).fetchall()
        matched_ids = {row["document_id"] for row in rows}
        if len(matched_ids) > 1:
            return AddResult("conflict", document["document_id"], False, "dedupe_keys_point_to_multiple_documents")
        if matched_ids:
            existing_id = next(iter(matched_ids))
            row = self.connection.execute(
                "SELECT canonical_json, content_hash FROM records WHERE run_id = ? AND document_id = ?",
                (run_id, existing_id),
            ).fetchone()
            existing = json.loads(row["canonical_json"])
            if row["content_hash"] != document["text"]["content_hash"]:
                return AddResult("conflict", existing_id, False, "same_identity_but_different_content")
            added_assignment = _merge_classifications(existing["classifications"], document["classifications"])
            _merge_provenance(existing["provenance"], document["provenance"])
            with self.connection:
                self.connection.execute(
                    "UPDATE records SET canonical_json = ?, updated_at = ? WHERE run_id = ? AND document_id = ?",
                    (json.dumps(existing, ensure_ascii=False, sort_keys=True), utc_now(), run_id, existing_id),
                )
                for key in keys:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO dedupe_keys (run_id, dedupe_key, document_id) VALUES (?, ?, ?)",
                        (run_id, key, existing_id),
                    )
            return AddResult("duplicate", existing_id, added_assignment, "exact_same_source_duplicate")
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO records (
                    run_id, document_id, source_name, source_record_id, content_hash,
                    canonical_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    document["document_id"],
                    document["source"]["name"],
                    document["source"]["record_id"],
                    document["text"]["content_hash"],
                    json.dumps(document, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            for key in keys:
                self.connection.execute(
                    "INSERT INTO dedupe_keys (run_id, dedupe_key, document_id) VALUES (?, ?, ?)",
                    (run_id, key, document["document_id"]),
                )
        return AddResult("accepted", document["document_id"], True)

    def add_rejection(
        self,
        run_id: str,
        *,
        category_id: str,
        source_name: str,
        raw_locator: str,
        title: str,
        reason: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO rejections (
                    run_id, category_id, source_name, raw_locator, title, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, category_id, source_name, raw_locator, title, reason, utc_now()),
            )

    def list_documents(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT canonical_json FROM records WHERE run_id = ? ORDER BY document_id",
            (run_id,),
        ).fetchall()
        return [json.loads(row["canonical_json"]) for row in rows]

    def list_rejections(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT category_id, source_name, raw_locator, title, reason, created_at
               FROM rejections WHERE run_id = ? ORDER BY id""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY category_id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def category_count(self, run_id: str, category_id: str) -> int:
        count = 0
        for document in self.list_documents(run_id):
            if any(item.get("category_id") == category_id for item in document["classifications"]):
                count += 1
        return count

    def metrics(self, run_id: str, known_category_ids: set[str]) -> dict[str, Any]:
        documents = self.list_documents(run_id)
        category_counts = {category_id: 0 for category_id in sorted(known_category_ids)}
        for document in documents:
            assigned: set[str] = set()
            for assignment in document.get("classifications", []):
                category_id = assignment.get("category_id")
                if category_id in category_counts and category_id not in assigned:
                    category_counts[category_id] += 1
                    assigned.add(category_id)
        tasks = self.list_tasks(run_id)
        rejection_rows = self.connection.execute(
            "SELECT reason, COUNT(*) AS count FROM rejections WHERE run_id = ? GROUP BY reason ORDER BY reason",
            (run_id,),
        ).fetchall()
        result = {
            "unique_documents": len(documents),
            "categories_with_documents": sum(count > 0 for count in category_counts.values()),
            "category_counts": category_counts,
            "seen": sum(task["seen"] for task in tasks),
            "accepted_assignments": sum(category_counts.values()),
            "rejected": sum(task["rejected"] for task in tasks),
            "rejection_reason_counts": {row["reason"]: row["count"] for row in rejection_rows},
            "duplicates": sum(task["duplicates"] for task in tasks),
            "conflicts": sum(task["conflicts"] for task in tasks),
            "verified_classification_assignments": sum(
                bool(assignment.get("verified"))
                for document in documents
                for assignment in document.get("classifications", [])
            ),
            "unverified_classification_assignments": sum(
                not bool(assignment.get("verified"))
                for document in documents
                for assignment in document.get("classifications", [])
            ),
            "tasks": tasks,
        }
        return result

    def _event(self, run_id: str, status: str, message: str, details: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events (run_id, created_at, status, message, details_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, utc_now(), status, message, json.dumps(details, ensure_ascii=False, sort_keys=True)),
        )


def _merge_unique_dicts(target: list[dict[str, Any]], incoming: list[dict[str, Any]], key: str) -> bool:
    seen = {item.get(key) for item in target}
    added = False
    for item in incoming:
        if item.get(key) not in seen:
            target.append(item)
            seen.add(item.get(key))
            added = True
    return added


def _merge_classifications(target: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> bool:
    by_category = {item.get("category_id"): item for item in target}
    added = False
    for item in incoming:
        category_id = item.get("category_id")
        existing = by_category.get(category_id)
        if existing is None:
            target.append(item)
            by_category[category_id] = item
            added = True
            continue
        existing_codes = list(existing.get("source_clc_codes", []))
        for code in item.get("source_clc_codes", []):
            if code not in existing_codes:
                existing_codes.append(code)
        incoming_is_stronger = (
            bool(item.get("verified")) and not bool(existing.get("verified"))
        ) or float(item.get("confidence", 0)) > float(existing.get("confidence", 0))
        if incoming_is_stronger:
            existing.update(item)
        existing["verified"] = bool(existing.get("verified")) or bool(item.get("verified"))
        existing["confidence"] = max(
            float(existing.get("confidence", 0)),
            float(item.get("confidence", 0)),
        )
        existing["source_clc_codes"] = existing_codes
    return added


def _merge_provenance(target: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> bool:
    def identity(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("raw_hash"),
            item.get("batch_id"),
            item.get("query_text"),
            item.get("raw_locator"),
        )

    seen = {identity(item) for item in target}
    added = False
    for item in incoming:
        key = identity(item)
        if key not in seen:
            target.append(item)
            seen.add(key)
            added = True
    return added
