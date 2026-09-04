"""发布后自动导入：把采集发布版本同步进 MedGraph 的 data/documents.db。

采集 Agent 的规范输出是 collector/data/published/generations/<run_id>/documents.jsonl；
而 MedGraph 下游（预处理 → 信息抽取）读的是 MedGraph/data/documents.db 的扁平表。
本模块在发布成功后，把规范文档转成旧抽取器的九字段记录，再按 document_id 幂等 upsert 入库，
使采集结果跑完即可直接进入预处理/抽取流水线。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# collector/ 的父目录即 MedGraph 项目根
MEDGRAPH_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS_DB = MEDGRAPH_ROOT / "data" / "documents.db"

# 复用 MedGraph 的 store_documents（建表/upsert 逻辑不重复实现）；确保 MedGraph 根可导入
if str(MEDGRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDGRAPH_ROOT))

from db import store_documents  # noqa: E402
from .legacy import to_legacy_records  # noqa: E402


def read_documents_jsonl(documents_path: Path) -> list[dict[str, Any]]:
    """读取规范 schema 的 documents.jsonl（每行一个 JSON 对象）。"""
    documents: list[dict[str, Any]] = []
    with documents_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                documents.append(json.loads(line))
    return documents


def import_published_generation(
    documents_path: Path,
    db_path: Path = DOCUMENTS_DB,
) -> int:
    """把发布版本的 documents.jsonl 导入 documents.db，返回实际写入/更新的条数。"""
    documents = read_documents_jsonl(documents_path)
    if not documents:
        return 0
    records = to_legacy_records(documents)
    return store_documents.import_records(records, db_path=db_path)
