"""JSON configuration with paths resolved relative to the project root."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigurationError
from .utils import stable_fingerprint


@dataclass
class CollectionSettings:
    project_root: Path
    catalog_path: Path
    state_db: Path
    output_root: Path
    default_source: str
    min_categories: int
    min_documents: int
    min_per_category: int
    min_text_chars: int
    page_size: int
    source_options: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, config_path: Path) -> "CollectionSettings":
        config_path = config_path.resolve()
        if not config_path.exists():
            raise ConfigurationError(f"配置文件不存在: {config_path}")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigurationError(f"无法读取配置文件 {config_path}: {exc}") from exc
        project_root = config_path.parent.parent
        gates = raw.get("quality_gates", {})
        settings = cls(
            project_root=project_root,
            catalog_path=_resolve(project_root, raw.get("catalog", "configs/clc_r_categories.csv")),
            state_db=_resolve(project_root, raw.get("state_db", "data/state/collection.sqlite3")),
            output_root=_resolve(project_root, raw.get("output_root", "data/published")),
            default_source=str(raw.get("default_source", "mediawiki")),
            min_categories=int(gates.get("min_categories", 100)),
            min_documents=int(gates.get("min_documents", 3000)),
            min_per_category=int(gates.get("min_per_category", 1)),
            min_text_chars=int(gates.get("min_text_chars", 40)),
            page_size=int(raw.get("page_size", 25)),
            source_options=dict(raw.get("sources", {})),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.min_categories <= 0 or self.min_documents <= 0 or self.min_per_category <= 0:
            raise ConfigurationError("质量门禁数值必须大于 0")
        if self.min_text_chars <= 0 or self.page_size <= 0:
            raise ConfigurationError("min_text_chars 和 page_size 必须大于 0")
        if self.default_source not in self.source_options:
            raise ConfigurationError(f"默认数据源没有配置: {self.default_source}")

    def source_config(self, source_name: str) -> dict[str, Any]:
        if source_name not in self.source_options:
            raise ConfigurationError(f"数据源没有配置: {source_name}")
        return dict(self.source_options[source_name])

    def apply_overrides(
        self,
        *,
        min_categories: Optional[int] = None,
        min_documents: Optional[int] = None,
        min_per_category: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> None:
        if min_categories is not None:
            self.min_categories = min_categories
        if min_documents is not None:
            self.min_documents = min_documents
        if min_per_category is not None:
            self.min_per_category = min_per_category
        if page_size is not None:
            self.page_size = page_size
        self.validate()

    def fingerprint_payload(self, source_name: str, category_ids: list[str]) -> dict[str, Any]:
        safe_source = {
            key: value
            for key, value in self.source_config(source_name).items()
            if key.lower() not in {"cookie", "cookies", "token", "password", "secret", "api_key"}
        }
        return {
            "source": source_name,
            "source_options": safe_source,
            "category_ids": category_ids,
            "output_root": str(self.output_root.resolve()),
            "min_categories": self.min_categories,
            "min_documents": self.min_documents,
            "min_per_category": self.min_per_category,
            "min_text_chars": self.min_text_chars,
            "page_size": self.page_size,
        }

    def fingerprint(self, source_name: str, category_ids: list[str]) -> str:
        return stable_fingerprint(self.fingerprint_payload(source_name, category_ids))


def _resolve(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()
