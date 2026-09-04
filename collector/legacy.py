"""Compatibility export for the older rule extractor's nine-field JSON list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import atomic_write_text


def to_legacy_records(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把规范 schema 的文档列表转成旧抽取器的九字段记录（供导出 / 导入 documents.db 复用）。"""
    records: list[dict[str, Any]] = []
    for document in documents:
        classifications = document.get("classifications", [])
        provenance = document.get("provenance", [])
        records.append(
            {
                "document_id": document["document_id"],
                "category_ids": [item["category_id"] for item in classifications],
                "title": document["bibliography"]["title"],
                "content": document["text"]["content"],
                "source_url": document["source"].get("url", ""),
                "license": document["source"].get("rights_statement", "unknown"),
                "collected_at": provenance[0].get("collected_at", "") if provenance else "",
                "content_hash": document["text"]["content_hash"],
                "quality_score": document["quality"]["score"],
            }
        )
    return records


def export_legacy(documents_path: Path, output_path: Path) -> int:
    records = []
    with documents_path.open("r", encoding="utf-8") as stream:
        documents = [
            json.loads(line)
            for line in stream
            if line.strip()
        ]
    records = to_legacy_records(documents)
    atomic_write_text(output_path, json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    return len(records)
