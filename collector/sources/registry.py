"""Construct source adapters from the project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import CollectionSettings
from ..errors import ConfigurationError
from ..models import Category
from .base import SourceAdapter
from .cnki import CNKIExportAdapter
from .mediawiki import MediaWikiAdapter


def build_adapter(
    source_name: str,
    settings: CollectionSettings,
    categories: list[Category],
    *,
    overrides: dict[str, Any] | None = None,
) -> SourceAdapter:
    options = settings.source_config(source_name)
    options.update(overrides or {})
    if source_name == "cnki":
        return CNKIExportAdapter(
            input_dir=_path(settings.project_root, options.get("input_dir", "data/inbox/cnki")),
            batch_manifest=_path(
                settings.project_root,
                options.get("batch_manifest", "data/inbox/cnki/batches.csv"),
            ),
            known_category_ids={category.category_id for category in categories},
            category_clc_codes={category.category_id: category.clc_code for category in categories},
            default_access_basis=str(options.get("access_basis", "institution-authorized CNKI export")),
            default_rights_statement=str(
                options.get("rights_statement", "仅限课程研究使用；遵守学校授权与知网许可。")
            ),
        )
    if source_name == "mediawiki":
        return MediaWikiAdapter(
            api_url=str(options.get("api_url", "https://zh.wikipedia.org/w/api.php")),
            user_agent=str(options.get("user_agent", "")),
            request_delay=float(options.get("request_delay", 0.5)),
            timeout=float(options.get("timeout", 20)),
            retries=int(options.get("retries", 4)),
            max_lag=int(options.get("max_lag", 5)),
            max_candidates_per_category=int(options.get("max_candidates_per_category", 100)),
            max_response_bytes=int(options.get("max_response_bytes", 8_000_000)),
            rights_statement=str(
                options.get(
                    "rights_statement",
                    "See the source page for license and attribution requirements.",
                )
            ),
        )
    raise ConfigurationError(f"未知数据源: {source_name}")


def _path(project_root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()
