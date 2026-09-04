"""Versioned publication: the current pointer changes only after all gates pass."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._lock import acquire_exclusive, release_exclusive  # 跨平台文件锁（fcntl/msvcrt）
from .errors import PublicationConflictError
from .models import PIPELINE_VERSION, SCHEMA_VERSION
from .utils import atomic_write_json, atomic_write_text, sha256_file, utc_now


EXPECTED_OUTPUT_PATHS = {
    "documents": "documents.jsonl",
    "rejections": "rejections.jsonl",
    "audit": "audit.json",
}
REQUIRED_OUTPUTS = set(EXPECTED_OUTPUT_PATHS)


def _validated_generations_root(output_root: Path, *, create: bool) -> tuple[Path, Path]:
    """Return a real, in-tree generations directory and reject symlink redirection."""

    resolved_output_root = output_root.resolve()
    generations_root = resolved_output_root / "generations"
    if create:
        generations_root.mkdir(parents=True, exist_ok=True)
    if (
        generations_root.is_symlink()
        or not generations_root.is_dir()
        or generations_root.resolve() != generations_root
    ):
        raise ValueError("generations 发布目录必须是 output_root 内的真实目录")
    return resolved_output_root, generations_root


def publish_generation(
    *,
    output_root: Path,
    run: dict[str, Any],
    documents: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    audit: dict[str, Any],
    source_descriptor: dict[str, Any],
    catalog_fingerprint: str,
    config_fingerprint: str,
    expected_previous_current_sha256: str | None = None,
) -> Path:
    output_root, generations_root = _validated_generations_root(output_root, create=True)
    generation_dir = generations_root / run["run_id"]
    if (
        generation_dir.is_symlink()
        or generation_dir.resolve() != generation_dir
        or generation_dir.parent != generations_root
        or generation_dir.name != run["run_id"]
    ):
        raise ValueError("run_id 形成了不安全的发布路径")
    if (generation_dir / "manifest.json").exists():
        raise PublicationConflictError(
            "同名 generation 已存在且不可覆盖；请恢复原 run 或使用新的 run ID"
        )
    generation_dir.mkdir(parents=True, exist_ok=True)
    if generation_dir.is_symlink() or generation_dir.resolve() != generation_dir:
        raise ValueError("generation 发布目录不得为符号链接")
    documents_path = generation_dir / "documents.jsonl"
    rejections_path = generation_dir / "rejections.jsonl"
    audit_path = generation_dir / "audit.json"
    manifest_path = generation_dir / "manifest.json"

    documents_text = "".join(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n" for document in documents
    )
    rejections_text = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rejections
    )
    atomic_write_text(documents_path, documents_text)
    atomic_write_text(rejections_path, rejections_text)
    atomic_write_json(audit_path, audit)
    manifest = {
        "schema_version": "1.0",
        "pipeline_version": PIPELINE_VERSION,
        "document_schema_version": SCHEMA_VERSION,
        "run_id": run["run_id"],
        "source": source_descriptor,
        "status": "SUCCEEDED",
        "created_at": run["created_at"],
        "published_at": utc_now(),
        "catalog_fingerprint": catalog_fingerprint,
        "config_fingerprint": config_fingerprint,
        "expected_previous_current_sha256": expected_previous_current_sha256,
        "metrics": audit["metrics"],
        "outputs": {
            "documents": {
                "path": "documents.jsonl",
                "sha256": sha256_file(documents_path),
                "records": len(documents),
            },
            "rejections": {
                "path": "rejections.jsonl",
                "sha256": sha256_file(rejections_path),
                "records": len(rejections),
            },
            "audit": {"path": "audit.json", "sha256": sha256_file(audit_path)},
        },
    }
    atomic_write_json(manifest_path, manifest)
    return generation_dir


def activate_generation(*, output_root: Path, generation_dir: Path, run_id: str) -> None:
    """Atomically activate a complete generation; safe to call repeatedly."""

    output_root, generations_root = _validated_generations_root(output_root, create=False)
    generation_dir = generation_dir.resolve()
    if generation_dir.parent != generations_root or generation_dir.name != run_id:
        raise ValueError("待激活 generation 路径与 run_id 不一致")
    manifest_path = generation_dir / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.resolve() != manifest_path:
        raise ValueError("manifest.json 不得为符号链接")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest_for_activation(manifest, generation_dir, run_id)

    generation_rel = generation_dir.relative_to(output_root)
    manifest_hash = sha256_file(manifest_path)
    pointer_path = output_root / "current.json"
    lock_path = output_root / ".publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ValueError("发布锁文件不得为符号链接")
    with lock_path.open("a+", encoding="utf-8") as lock_stream:
        acquire_exclusive(lock_stream)
        try:
            existing: dict[str, Any] = {}
            if pointer_path.exists():
                try:
                    existing = json.loads(pointer_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    existing = {}
            if (
                existing.get("run_id") == run_id
                and existing.get("generation") == generation_rel.as_posix()
                and existing.get("manifest_sha256") == manifest_hash
            ):
                return

            expected_previous = manifest.get("expected_previous_current_sha256")
            actual_previous = sha256_file(pointer_path) if pointer_path.is_file() else None
            if actual_previous != expected_previous:
                raise PublicationConflictError(
                    "current 已在本 run 准备发布后被其他 run 更新；为防止回滚，拒绝覆盖新版本"
                )
            pointer = {
                "schema_version": "1.0",
                "run_id": run_id,
                "generation": generation_rel.as_posix(),
                "manifest_sha256": manifest_hash,
                "updated_at": utc_now(),
            }
            atomic_write_json(pointer_path, pointer)
        finally:
            release_exclusive(lock_stream)


def _validate_manifest_for_activation(
    manifest: dict[str, Any],
    generation_dir: Path,
    run_id: str,
) -> None:
    if manifest.get("schema_version") != "1.0" or manifest.get("status") != "SUCCEEDED":
        raise ValueError("manifest schema_version/status 无效")
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        raise ValueError("manifest pipeline_version 无效")
    if manifest.get("document_schema_version") != SCHEMA_VERSION:
        raise ValueError("manifest document_schema_version 无效")
    if manifest.get("run_id") != run_id:
        raise ValueError("manifest 的 run_id 与待激活 run 不一致")
    if "expected_previous_current_sha256" not in manifest:
        raise ValueError("manifest 缺少发布前驱指纹")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != REQUIRED_OUTPUTS:
        raise ValueError("manifest 缺少 documents/rejections/audit 必需输出")
    resolved_outputs: set[Path] = set()
    physical_outputs: set[tuple[int, int]] = set()
    for name in REQUIRED_OUTPUTS:
        metadata = outputs[name]
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            raise ValueError(f"manifest 输出元数据无效: {name}")
        if metadata["path"] != EXPECTED_OUTPUT_PATHS[name]:
            raise ValueError(f"manifest 输出路径无效: {name}")
        if not isinstance(metadata.get("sha256"), str):
            raise ValueError(f"manifest 输出缺少哈希: {name}")
        declared_path = generation_dir / metadata["path"]
        output_path = declared_path.resolve()
        if declared_path.is_symlink() or output_path != declared_path:
            raise ValueError(f"manifest 输出不得为符号链接: {name}")
        if generation_dir not in output_path.parents:
            raise ValueError("manifest 包含不安全的输出路径")
        if output_path in resolved_outputs:
            raise ValueError("manifest 的必需输出不得指向同一文件")
        resolved_outputs.add(output_path)
        if not output_path.is_file() or sha256_file(output_path) != metadata["sha256"]:
            raise ValueError(f"待激活文件哈希不匹配: {metadata['path']}")
        file_stat = output_path.stat()
        physical_identity = (file_stat.st_dev, file_stat.st_ino)
        if physical_identity in physical_outputs:
            raise ValueError("manifest 的必需输出不得为同一物理文件的硬链接")
        physical_outputs.add(physical_identity)
        if name in {"documents", "rejections"}:
            expected_records = metadata.get("records")
            if (
                isinstance(expected_records, bool)
                or not isinstance(expected_records, int)
                or expected_records < 0
            ):
                raise ValueError(f"manifest 记录数无效: {name}")
            records = _read_jsonl_objects(output_path, name)
            if len(records) != expected_records:
                raise ValueError(f"manifest 记录数与文件不一致: {name}")
            if name == "documents":
                _validate_published_documents(records, manifest, run_id)

    try:
        audit = json.loads((generation_dir / EXPECTED_OUTPUT_PATHS["audit"]).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("audit.json 不是有效 JSON") from exc
    if (
        not isinstance(audit, dict)
        or audit.get("schema_version") != "1.0"
        or audit.get("passed") is not True
        or audit.get("metrics") != manifest.get("metrics")
    ):
        raise ValueError("audit.json 未通过或与 manifest metrics 不一致")


def _read_jsonl_objects(path: Path, name: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} 第 {line_number} 行不是有效 JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{name} 第 {line_number} 行必须是 JSON 对象")
            records.append(value)
    return records


def _validate_published_documents(
    documents: list[dict[str, Any]],
    manifest: dict[str, Any],
    run_id: str,
) -> None:
    document_ids: set[str] = set()
    source_metadata = manifest.get("source")
    expected_source = source_metadata.get("name") if isinstance(source_metadata, dict) else None
    for document in documents:
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id or document_id in document_ids:
            raise ValueError("documents.jsonl 包含空的或重复的 document_id")
        document_ids.add(document_id)
        collection = document.get("collection")
        source = document.get("source")
        if (
            document.get("schema_version") != SCHEMA_VERSION
            or not isinstance(collection, dict)
            or collection.get("run_id") != run_id
            or collection.get("pipeline_version") != PIPELINE_VERSION
            or not isinstance(source, dict)
            or source.get("name") != expected_source
        ):
            raise ValueError("documents.jsonl 的版本、run 或数据源契约不一致")


def resolve_current_generation(output_root: Path, *, verify_manifest_hash: bool = True) -> Path:
    output_root, generations_root = _validated_generations_root(output_root, create=False)
    pointer_path = output_root / "current.json"
    if pointer_path.is_symlink() or pointer_path.resolve() != pointer_path:
        raise ValueError("current.json 不得为符号链接")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    generation = (output_root / pointer["generation"]).resolve()
    if generation.parent != generations_root:
        raise ValueError("current.json 包含不安全的 generation 路径")
    manifest_path = generation / "manifest.json"
    if verify_manifest_hash and sha256_file(manifest_path) != pointer.get("manifest_sha256"):
        raise ValueError("current.json 中的 manifest 哈希与当前文件不一致")
    if verify_manifest_hash:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("run_id") != pointer.get("run_id") or generation.name != pointer.get("run_id"):
            raise ValueError("current.json、generation 与 manifest 的 run_id 不一致")
        _validate_manifest_for_activation(manifest, generation, str(pointer.get("run_id")))
    return generation
