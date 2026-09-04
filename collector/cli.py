"""Command-line interface for planning, collecting, resuming, auditing, and exporting."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .agent import CollectionAgent, validate_capacity
from .catalog import catalog_fingerprint, load_catalog, select_categories
from .config import CollectionSettings
from .errors import ConfigurationError, InputRequiredError, MedGraphError, ParseError, SourceError
from .importer import import_published_generation  # 发布后自动导入 documents.db
from .legacy import export_legacy
from .models import PIPELINE_VERSION, SCHEMA_VERSION, RunStatus
from .publisher import EXPECTED_OUTPUT_PATHS, resolve_current_generation
from .sources.registry import build_adapter
from .storage import StateStore
from .utils import atomic_write_text, sha256_file

# 默认配置固定在包内 configs/ 下，避免依赖运行时的当前工作目录
DEFAULT_CONFIG = Path(__file__).resolve().parent / "configs" / "collection.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medgraph",
        description="MedGraph CLC R 医学文本数据采集 Agent",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="项目配置文件")
    subcommands = parser.add_subparsers(dest="command", required=True)

    catalog_parser = subcommands.add_parser("catalog", help="验证 CLC R 类目表")
    catalog_parser.add_argument("--catalog", type=Path, default=None)
    catalog_parser.add_argument("--require-count", type=int, default=100)

    source_parser = subcommands.add_parser("source-check", help="只读检查数据源前置条件")
    source_parser.add_argument(
        "--source",
        choices=("cnki", "mediawiki"),
        default=None,
        help="数据源；省略时读取配置中的 default_source",
    )
    source_parser.add_argument("--input-dir", type=Path, default=None)
    source_parser.add_argument("--batch-manifest", type=Path, default=None)

    collect_parser = subcommands.add_parser("collect", help="生成计划或运行采集 Agent")
    collect_commands = collect_parser.add_subparsers(dest="collect_command", required=True)
    plan_parser = collect_commands.add_parser("plan", help="生成逐类别采集/导出计划")
    _add_collection_selection_args(plan_parser)
    plan_parser.add_argument("--output", type=Path, default=None)

    run_parser = collect_commands.add_parser("run", help="运行、续跑并按门禁发布")
    _add_collection_selection_args(run_parser)
    run_identity = run_parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id", default=None, help="自定义新 run ID")
    run_identity.add_argument("--resume", metavar="RUN_ID", default=None, help="续跑已有 run")
    run_parser.add_argument("--dry-run", action="store_true", help="执行到审计但不发布 current")
    run_parser.add_argument("--input-dir", type=Path, default=None, help="覆盖知网导出目录")
    run_parser.add_argument("--batch-manifest", type=Path, default=None, help="覆盖知网批次清单")

    status_parser = collect_commands.add_parser("status", help="查看 run 状态与检查点")
    status_parser.add_argument("run_id")

    audit_parser = subcommands.add_parser("audit", help="校验当前发布版本及文件哈希")
    audit_parser.add_argument("--output-root", type=Path, default=None)

    export_parser = subcommands.add_parser("export-legacy", help="导出旧抽取器兼容 JSON")
    export_parser.add_argument("--input", type=Path, default=None, help="canonical documents.jsonl")
    export_parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/exports/legacy/medical_sample.json"),
    )
    return parser


def _add_collection_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        choices=("cnki", "mediawiki"),
        default=None,
        help="数据源；省略时读取配置中的 default_source",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="可重复；填写 category_id 或 CLC code，默认全部启用类别",
    )
    parser.add_argument("--per-category", type=int, default=None)
    parser.add_argument("--min-categories", type=int, default=None)
    parser.add_argument("--min-documents", type=int, default=None)
    parser.add_argument("--min-per-category", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=None)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = CollectionSettings.load(args.config)
        if args.command == "catalog":
            return _catalog_command(settings, args)
        if args.command == "source-check":
            return _source_check(settings, args)
        if args.command == "collect" and args.collect_command == "plan":
            return _collect_plan(settings, args)
        if args.command == "collect" and args.collect_command == "run":
            return _collect_run(settings, args)
        if args.command == "collect" and args.collect_command == "status":
            return _collect_status(settings, args.run_id)
        if args.command == "audit":
            return _audit_current(settings, args)
        if args.command == "export-legacy":
            return _export_legacy(settings, args)
        parser.error("未知命令")
    except InputRequiredError as exc:
        print(f"需要人工输入: {exc}", file=sys.stderr)
        return 3
    except (ConfigurationError, ParseError, SourceError, MedGraphError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 4
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 1


def _catalog_command(settings: CollectionSettings, args: argparse.Namespace) -> int:
    path = _project_path(settings, args.catalog) if args.catalog else settings.catalog_path
    categories = load_catalog(path, enabled_only=False)
    enabled = [category for category in categories if category.enabled]
    reviewed = [category for category in enabled if category.reviewed]
    result = {
        "catalog": str(path),
        "total": len(categories),
        "enabled": len(enabled),
        "enabled_and_reviewed": len(reviewed),
        "required": args.require_count,
        "passed": len(reviewed) >= args.require_count,
        "fingerprint": catalog_fingerprint(categories),
    }
    _print_json(result)
    return 0 if result["passed"] else 2


def _source_check(settings: CollectionSettings, args: argparse.Namespace) -> int:
    categories = load_catalog(settings.catalog_path)
    source = args.source or settings.default_source
    overrides = _source_overrides(args)
    adapter = build_adapter(source, settings, categories, overrides=overrides)
    _print_json(adapter.healthcheck())
    return 0


def _collect_plan(settings: CollectionSettings, args: argparse.Namespace) -> int:
    _apply_gate_args(settings, args)
    _validate_per_category(args.per_category)
    categories = select_categories(load_catalog(settings.catalog_path), args.category)
    source = args.source or settings.default_source
    output = _project_path(settings, args.output) if args.output else settings.project_root / "data/plans/collection_plan.csv"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=(
            "category_id",
            "clc_code",
            "clc_name",
            "target_count",
            "query_terms",
            "recommended_query",
            "suggested_export_file",
        ),
    )
    writer.writeheader()
    for category in categories:
        target = args.per_category or category.target_count
        writer.writerow(
            {
                "category_id": category.category_id,
                "clc_code": category.clc_code,
                "clc_name": category.clc_name,
                "target_count": target,
                "query_terms": "|".join(category.query_terms),
                "recommended_query": _query_hint(source, category.clc_code, category.query_terms),
                "suggested_export_file": f"{category.category_id}__batch001.txt" if source == "cnki" else "",
            }
        )
    atomic_write_text(output, buffer.getvalue())
    payload = {"source": source, "categories": len(categories), "plan": str(output)}
    if source == "cnki":
        batch_template = output.with_name(output.stem + "_batches_template.csv")
        atomic_write_text(batch_template, _cnki_batch_template(settings, categories))
        payload["batch_template"] = str(batch_template)
    _print_json(payload)
    return 0


def _collect_run(settings: CollectionSettings, args: argparse.Namespace) -> int:
    _apply_gate_args(settings, args)
    _validate_per_category(args.per_category)
    all_categories = load_catalog(settings.catalog_path)
    categories = select_categories(all_categories, args.category)
    validate_capacity(
        categories,
        min_categories=settings.min_categories,
        min_documents=settings.min_documents,
        min_per_category=settings.min_per_category,
        per_category=args.per_category,
    )
    source = args.source or settings.default_source
    adapter = build_adapter(source, settings, all_categories, overrides=_source_overrides(args))
    store = StateStore(settings.state_db)
    try:
        agent = CollectionAgent(
            settings=settings,
            categories=categories,
            adapter=adapter,
            store=store,
            per_category=args.per_category,
        )
        result = agent.run(
            run_id=args.resume or args.run_id,
            resume=bool(args.resume),
            dry_run=args.dry_run,
        )
        payload: dict[str, Any] = {
            "run_id": result.run_id,
            "status": result.status,
            "message": result.message,
            "metrics": result.metrics,
        }
        if result.generation_dir:
            payload["generation_dir"] = str(result.generation_dir)
        # 发布成功后自动把文档导入 MedGraph 的 documents.db，供预处理/抽取流水线直接使用
        if (
            result.status == RunStatus.SUCCEEDED.value
            and result.generation_dir is not None
        ):
            documents_path = result.generation_dir / "documents.jsonl"
            if documents_path.is_file():
                imported = import_published_generation(documents_path)
                payload["imported_to_documents_db"] = imported
        _print_json(payload)
        if result.status in {RunStatus.SUCCEEDED.value, RunStatus.SUCCEEDED_DRY_RUN.value}:
            return 0
        if result.status == RunStatus.WAITING_FOR_INPUT.value:
            return 3
        return 2
    finally:
        store.close()


def _collect_status(settings: CollectionSettings, run_id: str) -> int:
    store = StateStore(settings.state_db)
    try:
        run = store.get_run(run_id)
        run["tasks"] = store.list_tasks(run_id)
        _print_json(run)
        return 0
    finally:
        store.close()


def _audit_current(settings: CollectionSettings, args: argparse.Namespace) -> int:
    output_root = _project_path(settings, args.output_root) if args.output_root else settings.output_root
    checks: dict[str, dict[str, Any]] = {}
    try:
        pointer_path = output_root / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        generation = resolve_current_generation(output_root, verify_manifest_hash=False)
        manifest_path = generation / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _print_json({"passed": False, "error": f"无法读取当前发布结构: {exc}", "checks": checks})
        return 2
    checks = {
        "manifest_pointer": {
            "path": str(manifest_path),
            "expected": pointer.get("manifest_sha256"),
            "actual": sha256_file(manifest_path),
        },
        "run_identity": {
            "pointer": pointer.get("run_id"),
            "manifest": manifest.get("run_id"),
            "generation": generation.name,
        },
    }
    checks["manifest_pointer"]["passed"] = (
        checks["manifest_pointer"]["expected"] == checks["manifest_pointer"]["actual"]
    )
    checks["run_identity"]["passed"] = len(
        {
            checks["run_identity"]["pointer"],
            checks["run_identity"]["manifest"],
            checks["run_identity"]["generation"],
        }
    ) == 1
    outputs = manifest.get("outputs")
    required_outputs = set(EXPECTED_OUTPUT_PATHS)
    checks["manifest_contract"] = {
        "schema_version": manifest.get("schema_version"),
        "pipeline_version": manifest.get("pipeline_version"),
        "document_schema_version": manifest.get("document_schema_version"),
        "status": manifest.get("status"),
        "has_publication_base": "expected_previous_current_sha256" in manifest,
    }
    checks["manifest_contract"]["passed"] = (
        checks["manifest_contract"]["schema_version"] == "1.0"
        and checks["manifest_contract"]["pipeline_version"] == PIPELINE_VERSION
        and checks["manifest_contract"]["document_schema_version"] == SCHEMA_VERSION
        and checks["manifest_contract"]["status"] == "SUCCEEDED"
        and checks["manifest_contract"]["has_publication_base"]
    )
    checks["outputs_structure"] = {
        "required": sorted(required_outputs),
        "actual": sorted(outputs) if isinstance(outputs, dict) else [],
        "passed": isinstance(outputs, dict) and set(outputs) == required_outputs,
    }
    for name in sorted(required_outputs):
        check_name = "output_" + name
        metadata = outputs.get(name) if isinstance(outputs, dict) else None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            checks[check_name] = {"passed": False, "error": "输出元数据缺少相对路径"}
            continue
        if metadata["path"] != EXPECTED_OUTPUT_PATHS[name]:
            checks[check_name] = {
                "path": metadata["path"],
                "expected_path": EXPECTED_OUTPUT_PATHS[name],
                "passed": False,
                "error": "输出路径与 manifest 契约不一致",
            }
            continue
        path = (generation / metadata["path"]).resolve()
        if generation not in path.parents or not path.is_file():
            checks[check_name] = {
                "path": str(path),
                "expected": metadata.get("sha256"),
                "actual": None,
                "passed": False,
                "error": "输出路径越界或文件不存在",
            }
            continue
        checks[check_name] = {
            "path": str(path),
            "expected": metadata.get("sha256"),
            "actual": sha256_file(path),
        }
        checks[check_name]["passed"] = checks[check_name]["expected"] == checks[check_name]["actual"]
    try:
        resolve_current_generation(output_root, verify_manifest_hash=True)
        checks["full_generation_contract"] = {"passed": True}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        checks["full_generation_contract"] = {"passed": False, "error": str(exc)}
    passed = all(item["passed"] for item in checks.values())
    _print_json({"run_id": manifest.get("run_id"), "passed": passed, "checks": checks})
    return 0 if passed else 2


def _export_legacy(settings: CollectionSettings, args: argparse.Namespace) -> int:
    documents = _project_path(settings, args.input) if args.input else resolve_current_generation(settings.output_root) / "documents.jsonl"
    output = _project_path(settings, args.output)
    count = export_legacy(documents, output)
    _print_json({"input": str(documents), "output": str(output), "records": count})
    return 0


def _apply_gate_args(settings: CollectionSettings, args: argparse.Namespace) -> None:
    settings.apply_overrides(
        min_categories=args.min_categories,
        min_documents=args.min_documents,
        min_per_category=args.min_per_category,
        page_size=args.page_size,
    )


def _validate_per_category(value: Optional[int]) -> None:
    if value is not None and value <= 0:
        raise ConfigurationError("--per-category 必须大于 0")


def _source_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides = {}
    if getattr(args, "input_dir", None):
        overrides["input_dir"] = str(args.input_dir.resolve())
    if getattr(args, "batch_manifest", None):
        overrides["batch_manifest"] = str(args.batch_manifest.resolve())
    return overrides


def _project_path(settings: CollectionSettings, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (settings.project_root / path).resolve()


def _query_hint(source: str, clc_code: str, terms: tuple[str, ...]) -> str:
    if source == "cnki":
        return f"高级检索：中图分类号={clc_code}；主题可辅以 {' OR '.join(terms)}"
    return " OR ".join(terms)


def _cnki_batch_template(settings: CollectionSettings, categories: list[Any]) -> str:
    options = settings.source_config("cnki")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=("batch_id", "file", "category_id", "query_text", "access_basis", "rights_statement"),
    )
    writer.writeheader()
    for category in categories:
        writer.writerow(
            {
                "batch_id": f"cnki_{category.category_id}_001",
                "file": f"{category.category_id}__batch001.txt",
                "category_id": category.category_id,
                "query_text": _query_hint("cnki", category.clc_code, category.query_terms),
                "access_basis": options.get("access_basis", ""),
                "rights_statement": options.get("rights_statement", ""),
            }
        )
    return buffer.getvalue()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
