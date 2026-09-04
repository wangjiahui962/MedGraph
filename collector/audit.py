"""Corpus acceptance gates used before publication and during later review."""

from __future__ import annotations

from typing import Any

from .models import Category
from .utils import utc_now


def build_audit(
    metrics: dict[str, Any],
    categories: list[Category],
    *,
    min_categories: int,
    min_documents: int,
    min_per_category: int,
) -> dict[str, Any]:
    category_counts = metrics["category_counts"]
    reviewed_ids = {category.category_id for category in categories if category.reviewed}
    eligible_counts = {
        category_id: count
        for category_id, count in category_counts.items()
        if category_id in reviewed_ids and count >= min_per_category
    }
    unreviewed_with_data = {
        category_id: count
        for category_id, count in category_counts.items()
        if category_id not in reviewed_ids and count > 0
    }
    gates = {
        "minimum_unique_documents": {
            "required": min_documents,
            "actual": metrics["unique_documents"],
            "passed": metrics["unique_documents"] >= min_documents,
        },
        "minimum_reviewed_clc_categories": {
            "required": min_categories,
            "actual": len(eligible_counts),
            "minimum_documents_each": min_per_category,
            "passed": len(eligible_counts) >= min_categories,
        },
        "no_conflicts": {
            "required": 0,
            "actual": metrics["conflicts"],
            "passed": metrics["conflicts"] == 0,
        },
    }
    shortfalls = []
    for category in categories:
        actual = category_counts.get(category.category_id, 0)
        if actual < min_per_category:
            shortfalls.append(
                {
                    "category_id": category.category_id,
                    "clc_code": category.clc_code,
                    "clc_name": category.clc_name,
                    "required": min_per_category,
                    "actual": actual,
                }
            )
    return {
        "schema_version": "1.0",
        "audited_at": utc_now(),
        "passed": all(gate["passed"] for gate in gates.values()),
        "gates": gates,
        "metrics": metrics,
        "shortfalls": shortfalls,
        "unreviewed_categories_with_documents": unreviewed_with_data,
    }
