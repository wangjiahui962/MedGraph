"""Stateful collection Agent with checkpoints, decisions, gates, and publish."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import build_audit
from .catalog import catalog_fingerprint
from .config import CollectionSettings
from .errors import (
    ConfigurationError,
    InputRequiredError,
    MedGraphError,
    ParseError,
    PublicationConflictError,
    SourceError,
)
from .models import PIPELINE_VERSION, SCHEMA_VERSION, Category, RunStatus
from .normalize import RejectedCandidate, normalize_candidate
from .publisher import activate_generation, publish_generation
from .sources.base import SourceAdapter
from .storage import StateStore
from .utils import normalize_text, sha256_file, stable_fingerprint


@dataclass
class RunResult:
    run_id: str
    status: str
    message: str
    metrics: dict[str, Any]
    generation_dir: Optional[Path] = None


class CollectionAgent:
    """Coordinates a source without coupling downstream code to that source."""

    def __init__(
        self,
        *,
        settings: CollectionSettings,
        categories: list[Category],
        adapter: SourceAdapter,
        store: StateStore,
        per_category: Optional[int] = None,
    ) -> None:
        if not categories:
            raise ConfigurationError("采集 Agent 至少需要一个类别")
        if per_category is not None and per_category <= 0:
            raise ConfigurationError("per_category 必须大于 0")
        self.settings = settings
        # The run fingerprint treats category selection as a set, so execution
        # must use the same canonical order to keep resume results deterministic.
        self.categories = sorted(categories, key=lambda category: category.category_id)
        self.adapter = adapter
        self.store = store
        self.per_category = per_category
        self.catalog_by_id = {category.category_id: category for category in self.categories}

    def run(
        self,
        *,
        run_id: Optional[str] = None,
        resume: bool = False,
        dry_run: bool = False,
    ) -> RunResult:
        run_id = run_id or _new_run_id()
        _validate_run_id(run_id)
        with self.store.run_lock(run_id):
            return self._run_locked(run_id=run_id, resume=resume, dry_run=dry_run)

    def _run_locked(self, *, run_id: str, resume: bool, dry_run: bool) -> RunResult:
        category_ids = sorted(category.category_id for category in self.categories)
        config_payload = self.settings.fingerprint_payload(self.adapter.name, category_ids)
        config_payload["per_category_override"] = self.per_category
        config_payload["adapter_resume_identity"] = self.adapter.resume_identity()
        config_payload["pipeline_version"] = PIPELINE_VERSION
        config_payload["document_schema_version"] = SCHEMA_VERSION
        config_hash = stable_fingerprint(config_payload)
        catalog_hash = catalog_fingerprint(self.categories)
        if resume:
            run = self.store.require_resume_match(
                run_id,
                source_name=self.adapter.name,
                config_fingerprint=config_hash,
                catalog_fingerprint=catalog_hash,
                adapter_version=self.adapter.version,
            )
            if run["status"] == RunStatus.CREATED.value:
                self.store.transition(
                    run_id,
                    RunStatus.PLANNING,
                    "resume_recovered_run_created_before_planning",
                )
                run = self.store.get_run(run_id)
            if run["status"] == RunStatus.SUCCEEDED.value:
                return RunResult(run_id, run["status"], "该 run 已完成，无需重复执行", run["metrics"])
            if run["status"] == RunStatus.SUCCEEDED_DRY_RUN.value and dry_run:
                return RunResult(run_id, run["status"], "该 dry-run 已完成，无需重复执行", run["metrics"])
            if run["status"] == RunStatus.FAILED.value:
                raise MedGraphError("FAILED run 不允许直接续跑；请修复问题后创建新的 run")
            if run["status"] in {RunStatus.READY_TO_COMMIT.value, RunStatus.ACTIVATING.value}:
                recovered = self._recover_publication(run_id, run["status"])
                if recovered is not None:
                    return recovered
        else:
            self.store.create_run(
                run_id=run_id,
                source_name=self.adapter.name,
                config_fingerprint=config_hash,
                catalog_fingerprint=catalog_hash,
                adapter_version=self.adapter.version,
                min_categories=self.settings.min_categories,
                min_documents=self.settings.min_documents,
                min_per_category=self.settings.min_per_category,
            )
            self.store.transition(run_id, RunStatus.PLANNING, "collection_plan_validated")

        try:
            current = self.store.get_run(run_id)["status"]
            if current != RunStatus.ACQUIRING.value:
                self.store.transition(run_id, RunStatus.ACQUIRING, "source_acquisition_started")
            source_health = self.adapter.healthcheck()
            self._collect_categories(run_id)
            self.store.transition(run_id, RunStatus.NORMALIZING, "normalization_completed")
            self.store.transition(run_id, RunStatus.DEDUPING, "source_scoped_dedupe_completed")
            self.store.transition(run_id, RunStatus.VALIDATING, "quality_audit_started")
            metrics = self.store.metrics(run_id, set(self.catalog_by_id))
            audit = build_audit(
                metrics,
                self.categories,
                min_categories=self.settings.min_categories,
                min_documents=self.settings.min_documents,
                min_per_category=self.settings.min_per_category,
            )
            self.store.save_metrics(run_id, metrics)
            if not audit["passed"]:
                message = self._gap_message(metrics)
                self.store.transition(
                    run_id,
                    RunStatus.COMPLETED_WITH_GAPS,
                    "quality_gates_failed_no_publication",
                    {"gates": audit["gates"]},
                )
                return RunResult(
                    run_id,
                    RunStatus.COMPLETED_WITH_GAPS.value,
                    message,
                    metrics,
                )
            if dry_run:
                self.store.transition(run_id, RunStatus.SUCCEEDED_DRY_RUN, "dry_run_passed_no_publication")
                return RunResult(run_id, RunStatus.SUCCEEDED_DRY_RUN.value, "门禁通过；dry-run 未发布", metrics)
            publication_base = self._publication_base(run_id)
            self.store.transition(run_id, RunStatus.READY_TO_COMMIT, "all_gates_passed")
            run = self.store.get_run(run_id)
            generation_dir = publish_generation(
                output_root=self.settings.output_root,
                run=run,
                documents=self.store.list_documents(run_id),
                rejections=self.store.list_rejections(run_id),
                audit=audit,
                source_descriptor=source_health,
                catalog_fingerprint=catalog_hash,
                config_fingerprint=config_hash,
                expected_previous_current_sha256=publication_base,
            )
            self.store.transition(run_id, RunStatus.ACTIVATING, "generation_written_activation_started")
            return self._activate_publication(run_id, generation_dir, metrics)
        except (InputRequiredError, ParseError) as exc:
            current = self.store.get_run(run_id)["status"]
            if current != RunStatus.WAITING_FOR_INPUT.value:
                self.store.transition(
                    run_id,
                    RunStatus.WAITING_FOR_INPUT,
                    "human_input_required",
                    {"reason": str(exc)},
                )
            return RunResult(
                run_id,
                RunStatus.WAITING_FOR_INPUT.value,
                str(exc),
                self.store.metrics(run_id, set(self.catalog_by_id)),
            )
        except Exception as exc:
            self.store.fail(run_id, _safe_error(exc))
            raise

    def _recover_publication(self, run_id: str, status: str) -> RunResult | None:
        generation_dir = (self.settings.output_root / "generations" / run_id).resolve()
        if not (generation_dir / "manifest.json").is_file():
            if status == RunStatus.ACTIVATING.value:
                self.store.transition(
                    run_id,
                    RunStatus.ACQUIRING,
                    "activation_generation_missing_rebuild_started",
                )
            return None
        if status == RunStatus.READY_TO_COMMIT.value:
            self.store.transition(run_id, RunStatus.ACTIVATING, "publication_recovery_started")
        return self._activate_publication(
            run_id,
            generation_dir,
            self.store.get_run(run_id)["metrics"],
        )

    def _activate_publication(
        self,
        run_id: str,
        generation_dir: Path,
        metrics: dict[str, Any],
    ) -> RunResult:
        try:
            activate_generation(
                output_root=self.settings.output_root,
                generation_dir=generation_dir,
                run_id=run_id,
            )
            self.store.transition(run_id, RunStatus.SUCCEEDED, "generation_published")
        except PublicationConflictError as exc:
            self.store.fail(run_id, _safe_error(exc))
            return RunResult(
                run_id,
                RunStatus.FAILED.value,
                f"发布被拒绝，较新的 current 已保留：{exc}",
                metrics,
                generation_dir,
            )
        except Exception as exc:
            return RunResult(
                run_id,
                RunStatus.ACTIVATING.value,
                "generation 已完整写入，但 current 激活或状态确认尚未完成；"
                f"请使用相同参数 --resume 重试。原因: {_safe_error(exc)}",
                metrics,
                generation_dir,
            )
        return RunResult(
            run_id,
            RunStatus.SUCCEEDED.value,
            "质量门禁通过，已原子发布新版本",
            metrics,
            generation_dir,
        )

    def _publication_base(self, run_id: str) -> str | None:
        run = self.store.get_run(run_id)
        if run["publication_base_captured"]:
            return run["publication_base_sha256"]
        pointer_path = self.settings.output_root / "current.json"
        current_hash = sha256_file(pointer_path) if pointer_path.is_file() else None
        return self.store.capture_publication_base(run_id, current_hash)

    def _gap_message(self, metrics: dict[str, Any]) -> str:
        prefix = "采集完成但门禁未通过；旧版本未被覆盖。"
        if metrics.get("conflicts", 0) > 0:
            return (
                prefix
                + "当前 run 存在身份冲突，补采不能清零；请检查冲突输入或映射，修正后使用新的 run ID。"
            )
        targets_reached = all(
            metrics.get("category_counts", {}).get(category.category_id, 0)
            >= (self.per_category or category.target_count)
            for category in self.categories
        )
        if targets_reached and metrics.get("unique_documents", 0) < self.settings.min_documents:
            return (
                prefix
                + "所有类别已达到本 run 的采集上限，但唯一文献仍不足；"
                "请提高 --per-category 并使用新的 run ID。"
            )
        if self.adapter.name == "cnki":
            return prefix + "可在 batches.csv 末尾追加新批次后，使用完全相同参数 --resume。"
        source_failed = any(
            task.get("status") == "SOURCE_FAILED" for task in metrics.get("tasks", [])
        )
        if source_failed:
            return (
                prefix
                + "存在网络或数据源失败；请等待限流恢复或检查网络后，使用完全相同参数 --resume。"
            )
        return (
            prefix
            + "请检查类别缺口和拒收原因；若检索结果已耗尽，复核 query_terms 或提高采集容量后，"
            "使用新的 run ID。"
        )

    def _collect_categories(self, run_id: str) -> None:
        for category in self.categories:
            if self._overall_gate_already_met(run_id):
                break
            task = self.store.ensure_task(run_id, category.category_id)
            target = self.per_category or category.target_count
            accepted_now = self.store.category_count(run_id, category.category_id)
            if accepted_now >= target:
                self.store.checkpoint_task(
                    run_id,
                    category.category_id,
                    status="COMPLETE",
                    cursor=task["cursor"],
                    seen=task["seen"],
                    accepted=accepted_now,
                    rejected=task["rejected"],
                    duplicates=task["duplicates"],
                    conflicts=task["conflicts"],
                )
                continue
            cursor = task["cursor"]
            seen = task["seen"]
            rejected = task["rejected"]
            duplicates = task["duplicates"]
            conflicts = task["conflicts"]
            last_cursor: str | None = None
            while accepted_now < target:
                try:
                    page = self.adapter.collect_page(
                        category,
                        cursor,
                        min(self.settings.page_size, target - accepted_now),
                    )
                except ParseError as exc:
                    self.store.checkpoint_task(
                        run_id,
                        category.category_id,
                        status="WAITING_FOR_INPUT",
                        cursor=cursor,
                        seen=seen,
                        accepted=accepted_now,
                        rejected=rejected,
                        duplicates=duplicates,
                        conflicts=conflicts,
                        error=_safe_error(exc),
                    )
                    raise
                except SourceError as exc:
                    self.store.checkpoint_task(
                        run_id,
                        category.category_id,
                        status="SOURCE_FAILED",
                        cursor=cursor,
                        seen=seen,
                        accepted=accepted_now,
                        rejected=rejected,
                        duplicates=duplicates,
                        conflicts=conflicts,
                        error=_safe_error(exc),
                    )
                    break
                if page.raw_count == 0 and not page.exhausted:
                    raise SourceError(f"{category.category_id} 返回空页但未标记结束", retryable=False)
                for record in page.records:
                    seen += 1
                    try:
                        document, keys = normalize_candidate(
                            record,
                            category,
                            run_id=run_id,
                            collector_version=self.adapter.version,
                            min_text_chars=self.settings.min_text_chars,
                        )
                    except RejectedCandidate as exc:
                        rejected += 1
                        self.store.add_rejection(
                            run_id,
                            category_id=category.category_id,
                            source_name=record.source_name,
                            raw_locator=record.raw_locator,
                            title=normalize_text(record.title)[:300],
                            reason=exc.reason,
                        )
                        continue
                    result = self.store.add_document(run_id, document, keys)
                    if result.disposition == "accepted":
                        accepted_now += 1
                    elif result.disposition == "duplicate":
                        duplicates += 1
                        if result.added_assignment:
                            accepted_now += 1
                    else:
                        conflicts += 1
                        rejected += 1
                        self.store.add_rejection(
                            run_id,
                            category_id=category.category_id,
                            source_name=record.source_name,
                            raw_locator=record.raw_locator,
                            title=normalize_text(record.title)[:300],
                            reason="conflict:" + result.reason,
                        )
                next_cursor = page.next_cursor
                status = "COMPLETE" if accepted_now >= target else ("EXHAUSTED" if page.exhausted else "RUNNING")
                self.store.checkpoint_task(
                    run_id,
                    category.category_id,
                    status=status,
                    cursor=next_cursor,
                    seen=seen,
                    accepted=accepted_now,
                    rejected=rejected,
                    duplicates=duplicates,
                    conflicts=conflicts,
                )
                if page.exhausted or accepted_now >= target:
                    break
                if next_cursor is None or next_cursor == cursor or next_cursor == last_cursor:
                    raise SourceError(f"{category.category_id} 的分页游标没有前进", retryable=False)
                last_cursor, cursor = cursor, next_cursor

    def _overall_gate_already_met(self, run_id: str) -> bool:
        metrics = self.store.metrics(run_id, set(self.catalog_by_id))
        categories_ready = sum(
            count >= self.settings.min_per_category and self.catalog_by_id[category_id].reviewed
            for category_id, count in metrics["category_counts"].items()
        )
        return (
            metrics["unique_documents"] >= self.settings.min_documents
            and categories_ready >= self.settings.min_categories
        )


def validate_capacity(
    categories: list[Category],
    *,
    min_categories: int,
    min_documents: int,
    min_per_category: int,
    per_category: Optional[int],
) -> None:
    if per_category is not None and per_category <= 0:
        raise MedGraphError("--per-category 必须大于 0")
    reviewed = [category for category in categories if category.reviewed]
    if len(reviewed) < min_categories:
        raise MedGraphError(f"只选择了 {len(reviewed)} 个已复核类别，门禁要求至少 {min_categories} 个")
    eligible = [
        category
        for category in reviewed
        if (per_category or category.target_count) >= min_per_category
    ]
    if len(eligible) < min_categories:
        raise MedGraphError(
            f"只有 {len(eligible)} 个类别的采集目标达到每类门禁 {min_per_category} 条，"
            f"但类别门禁要求至少 {min_categories} 个"
        )
    capacity = sum(per_category or category.target_count for category in reviewed)
    if capacity < min_documents:
        raise MedGraphError(f"理论采集上限只有 {capacity} 条，小于门禁 {min_documents} 条")


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{timestamp}_{secrets.token_hex(3)}"


def _validate_run_id(run_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ConfigurationError(
            "run ID 只能包含字母、数字、连字符和下划线，长度 1-64，且必须以字母或数字开头"
        )


def _safe_error(exc: Exception) -> str:
    message = normalize_text(str(exc))
    for marker in ("password=", "token=", "cookie=", "api_key="):
        if marker in message.lower():
            return f"{exc.__class__.__name__}: [敏感信息已隐藏]"
    return f"{exc.__class__.__name__}: {message[:1000]}"
