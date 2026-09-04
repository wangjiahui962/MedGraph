"""CLC R catalog loading and validation."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .errors import ConfigurationError
from .models import Category
from .utils import normalize_text, split_terms, stable_fingerprint


REQUIRED_COLUMNS = {
    "category_id",
    "clc_code",
    "clc_name",
    "parent_code",
    "query_terms",
    "target_count",
    "enabled",
    "authority_source",
    "reviewed",
}


def parse_bool(value: str, field_name: str, row_number: int) -> bool:
    normalized = normalize_text(value).lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ConfigurationError(f"类别表第 {row_number} 行的 {field_name} 必须为 true/false")


def load_catalog(path: Path, *, enabled_only: bool = True) -> list[Category]:
    if not path.exists():
        raise ConfigurationError(f"类别表不存在: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ConfigurationError(f"类别表缺少字段: {', '.join(sorted(missing))}")
        categories: list[Category] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                target_count = int(normalize_text(row["target_count"]))
            except ValueError as exc:
                raise ConfigurationError(f"类别表第 {row_number} 行 target_count 不是整数") from exc
            category = Category(
                category_id=normalize_text(row["category_id"]),
                clc_code=normalize_text(row["clc_code"]).upper(),
                clc_name=normalize_text(row["clc_name"]),
                parent_code=normalize_text(row["parent_code"]).upper(),
                query_terms=split_terms(row["query_terms"]),
                target_count=target_count,
                enabled=parse_bool(row["enabled"], "enabled", row_number),
                authority_source=normalize_text(row["authority_source"]),
                reviewed=parse_bool(row["reviewed"], "reviewed", row_number),
            )
            _validate_category(category, row_number)
            categories.append(category)
    _validate_uniqueness(categories)
    selected = [category for category in categories if category.enabled] if enabled_only else categories
    if not selected:
        raise ConfigurationError("类别表没有启用的类别")
    return selected


def _validate_category(category: Category, row_number: int) -> None:
    if not category.category_id:
        raise ConfigurationError(f"类别表第 {row_number} 行 category_id 为空")
    if not category.clc_code.startswith("R") or category.clc_code == "R":
        raise ConfigurationError(f"类别表第 {row_number} 行不是有效的 CLC R 子类: {category.clc_code}")
    if not category.clc_name:
        raise ConfigurationError(f"类别表第 {row_number} 行 clc_name 为空")
    if category.target_count <= 0:
        raise ConfigurationError(f"类别表第 {row_number} 行 target_count 必须大于 0")
    if not category.query_terms:
        raise ConfigurationError(f"类别表第 {row_number} 行至少要有一个检索词")
    if not category.authority_source:
        raise ConfigurationError(f"类别表第 {row_number} 行 authority_source 为空")


def _validate_uniqueness(categories: Iterable[Category]) -> None:
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()
    for category in categories:
        if category.category_id in seen_ids:
            raise ConfigurationError(f"category_id 重复: {category.category_id}")
        if category.clc_code in seen_codes:
            raise ConfigurationError(f"clc_code 重复: {category.clc_code}")
        seen_ids.add(category.category_id)
        seen_codes.add(category.clc_code)


def catalog_fingerprint(categories: Iterable[Category]) -> str:
    return stable_fingerprint(
        [category.to_dict() for category in sorted(categories, key=lambda item: item.category_id)]
    )


def select_categories(categories: list[Category], selectors: list[str]) -> list[Category]:
    if not selectors:
        return categories
    by_id = {category.category_id: category for category in categories}
    by_code = {category.clc_code: category for category in categories}
    selected: list[Category] = []
    unknown: list[str] = []
    for selector in selectors:
        category = by_id.get(selector) or by_code.get(selector.upper())
        if category is None:
            unknown.append(selector)
        elif category not in selected:
            selected.append(category)
    if unknown:
        raise ConfigurationError(f"未知类别: {', '.join(unknown)}")
    return selected
