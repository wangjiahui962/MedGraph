#!/usr/bin/env python3
"""
【本文件要完成的工作】将采集得到的文档写入 data/documents.db，支持增删改查。

1. 读取 data/raw/medical_sample.json（采集模块输出）。
2. 逐条插入到 data/documents.db 的 documents 表中：
   - 键标签与 medical_sample.json 保持一致（document_id/category_ids/title/content/
     source_url/license/collected_at/content_hash/quality_score）。
   - category_ids 列表序列化为 JSON 字符串存储。
3. 提供增删改查能力（对文档库）：
   - 新增：按 document_id 插入，重复则跳过或更新（upsert）。
   - 查询：支持按 document_id / title / category_ids 检索。
   - 更新：按 document_id 更新字段。
   - 删除：按 document_id 删除记录。
4. 可重复运行（幂等）：重复运行不产生重复数据，可通过 content_hash 或 document_id 去重。
5. 运行完成后打印导入的文档数量与库内总数，便于核对与 raw JSON 是否一致。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/raw/medical_sample.json"
DEFAULT_DB = ROOT / "data/documents.db"

# 繁转简（可选依赖 opencc，与 preprocess/preprocess.py 同一口径）：
# 前端“展开原文”展示的正文应和抽取原句一致为简体，故导出 documents.json 时统一转简体。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from preprocess.preprocess import _to_simplified
except Exception:  # 依赖缺失/导入异常时保留原文（繁体），流程不中断
    _to_simplified = None

# documents 表字段（与 medical_sample.json 键标签一致）
DOCUMENT_COLUMNS = [
    "document_id",
    "category_ids",
    "title",
    "content",
    "source_url",
    "license",
    "collected_at",
    "content_hash",
    "quality_score",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    """确保表已存在并返回连接（依赖 init_db 建表，此处兜底建表）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            document_id    TEXT PRIMARY KEY,
            category_ids   TEXT,
            title          TEXT,
            content        TEXT,
            source_url     TEXT,
            license        TEXT,
            collected_at   TEXT,
            content_hash   TEXT,
            quality_score  REAL
        )
    """)
    return conn


COLUMNS_SQL = ", ".join(DOCUMENT_COLUMNS)
PLACEHOLDERS_SQL = ", ".join("?" for _ in DOCUMENT_COLUMNS)


def insert_document(conn: sqlite3.Connection, record: dict[str, Any], update: bool = True) -> bool:
    """插入一条文档；update=True 时重复 document_id 走更新，否则跳过。返回是否写入。"""
    values = {key: record.get(key) for key in DOCUMENT_COLUMNS}
    if isinstance(values["category_ids"], list):
        values["category_ids"] = json.dumps(values["category_ids"], ensure_ascii=False)
    params = [values[c] for c in DOCUMENT_COLUMNS]
    if update:
        sql = (
            "INSERT INTO documents (" + COLUMNS_SQL + ") VALUES (" + PLACEHOLDERS_SQL + ")"
            " ON CONFLICT(document_id) DO UPDATE SET"
            " title = excluded.title, content = excluded.content, source_url = excluded.source_url,"
            " license = excluded.license, collected_at = excluded.collected_at,"
            " content_hash = excluded.content_hash, quality_score = excluded.quality_score"
        )
        return conn.execute(sql, params).rowcount > 0
    try:
        conn.execute(
            "INSERT INTO documents (" + COLUMNS_SQL + ") VALUES (" + PLACEHOLDERS_SQL + ")",
            params,
        )
        return True
    except sqlite3.IntegrityError:
        return False


def import_records(records: list[dict[str, Any]], db_path: Path = DEFAULT_DB, update: bool = True) -> int:
    """将采集记录批量写入文档库，返回实际写入/更新的条数。"""
    conn = _connect(db_path)
    try:
        with conn:
            return sum(1 for record in records if insert_document(conn, record, update))
    finally:
        conn.close()


def count_documents(db_path: Path = DEFAULT_DB) -> int:
    """查询文档库当前总数。"""
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()



def export_documents_json(db_path: Path = DEFAULT_DB, out_path: Path | None = None) -> int:
    """导出前端“文档原文”展示所需的精简 JSON（data/processed/documents.json）。

    只保留 document_id / title / content 三个字段；由 frontend/scripts/sync-data.mjs
    同步到 frontend/public/data/documents.json，供证据面板“展开”按钮按需加载。
    正文/标题会经 OpenCC t2s 统一繁体转简体（与抽取原句同一简体口径，避免前端
    出现繁体字）；未安装 opencc 时保留原文。

    运行：python db/store_documents.py --export-frontend
    """
    if out_path is None:
        out_path = ROOT / "data" / "processed" / "documents.json"
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT document_id, title, content FROM documents"
        ).fetchall()
    finally:
        conn.close()

    def _simp(value: str | None) -> str:
        """繁体转简体；opencc 缺失时原样返回。"""
        value = value or ""
        return _to_simplified(value) if _to_simplified is not None else value

    records = [
        {"document_id": rid, "title": _simp(title), "content": _simp(content)}
        for rid, title, content in rows
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {len(records)} documents (simplified) -> {out_path.relative_to(ROOT)}")
    return len(records)



def main() -> int:
    # --export-frontend：从文档库导出前端 documents.json
    if len(sys.argv) > 1 and sys.argv[1] == "--export-frontend":
        export_documents_json()
        return 0

    input_path = DEFAULT_INPUT
    if not input_path.is_file():
        print(f"ERROR: 输入文件不存在: {input_path}", file=sys.stderr)
        return 1
    records = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        print("ERROR: 输入 JSON 必须是文档列表", file=sys.stderr)
        return 2
    written = import_records(records)
    total = count_documents()
    print(f"Imported/updated {written} documents from {input_path.name}")
    print(f"Total documents in {DEFAULT_DB.name}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
