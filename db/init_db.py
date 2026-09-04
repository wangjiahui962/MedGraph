#!/usr/bin/env python3
"""
【本文件要完成的工作】初始化项目中的两个 SQLite 数据库文件。

1. 创建 data/documents.db（文档库）
   - 建表 documents，字段与 data/raw/medical_sample.json 中每条记录的键标签对应：
       document_id    TEXT PRIMARY KEY   -- 文档唯一编号，如 doc_000001
       category_ids   TEXT               -- 类别列表，JSON 字符串存储（如 ["respiratory"]）
       title          TEXT               -- 文档标题（疾病名）
       content        TEXT               -- 正文全文
       source_url     TEXT               -- 来源 URL
       license        TEXT               -- 许可标识，如 public-info
       collected_at   TEXT               -- 采集时间（ISO 8601）
       content_hash   TEXT               -- 正文 SHA-256 哈希，用于去重
       quality_score  REAL               -- 质量评分
   - 为 document_id 建立唯一索引，便于按编号增删改查。

2. 创建 data/triples.db（三元组库）
   - 建表 triples，字段与 data/processed/triples.json 中每条三元组的键标签对应：
       id                INTEGER PRIMARY KEY AUTOINCREMENT
       subject           TEXT             -- 主语（疾病）
       subject_type      TEXT             -- 主语类型，如 疾病
       relation          TEXT             -- 关系，如 常见症状/治疗/病因/不良反应/检查方法
       object            TEXT             -- 客体
       object_type       TEXT             -- 客体类型，如 症状/药物/治疗方法/疾病/检查方法
       source_document_id TEXT            -- 来源文档编号
       source_text       TEXT             -- 原文证据句
       confidence        REAL             -- 置信度
       layer             TEXT             -- 产出层：rule/deep_learning/llm
       trained           INTEGER          -- 参与模型训练的次数（每训练一次 +1，默认 0）
   - 为 (subject, relation, object, object_type) 建立唯一索引，避免重复三元组。

3. 库文件不存在时自动创建父目录（data/）与空库文件；已存在则不做破坏性操作。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS_DB = ROOT / "data/documents.db"
TRIPLES_DB = ROOT / "data/triples.db"

# documents 表结构，字段与 medical_sample.json 键标签一一对应
CREATE_DOCUMENTS_TABLE = """
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
);
"""

# triples 表结构，字段与 triples.json 键标签一一对应
CREATE_TRIPLES_TABLE = """
CREATE TABLE IF NOT EXISTS triples (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    subject            TEXT,
    subject_type       TEXT,
    relation           TEXT,
    object             TEXT,
    object_type        TEXT,
    source_document_id TEXT,
    source_text        TEXT,
    confidence         REAL,
    layer              TEXT,
    trained            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (subject, relation, object, object_type)
);
"""

# 抽取进度表（文档级增量“指针”，放在 documents.db 中）：
# 记录“已成功抽取过的文档及其内容哈希”，供 extraction/extract.py 增量跳过，
# 避免每次“提取现有数据”都重复对全部文档调用 LLM。
CREATE_EXTRACT_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS extract_state (
    document_id    TEXT PRIMARY KEY,
    content_hash   TEXT,             -- 抽取时该文档的内容哈希（与 documents.content_hash 一致）
    layer          TEXT,             -- 记录来源层（llm / seed）
    extracted_at   TEXT,             -- 最近成功抽取时间
    triple_count   INTEGER NOT NULL DEFAULT 0
);
"""


def init_documents_db(db_path: Path) -> None:
    """创建/连接文档库并建表（含抽取进度表），若已存在则保持不变。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_DOCUMENTS_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title)")
        conn.execute(CREATE_EXTRACT_STATE_TABLE)
        conn.commit()


def _migrate_triples(conn: sqlite3.Connection) -> None:
    """兼容旧库：triples 表缺少 trained 列时用 ALTER TABLE 补上（默认 0）。

    CREATE TABLE IF NOT EXISTS 不会给已存在的旧表加列，因此新字段需单独迁移；
    幂等：已存在该列则什么都不做。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(triples)")}
    if "trained" not in cols:
        conn.execute("ALTER TABLE triples ADD COLUMN trained INTEGER NOT NULL DEFAULT 0")


def init_triples_db(db_path: Path) -> None:
    """创建/连接三元组库并建表，若已存在则保持不变（缺失字段自动迁移补齐）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_TRIPLES_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_relation ON triples(relation)")
        _migrate_triples(conn)
        conn.commit()


def main() -> int:
    init_documents_db(DOCUMENTS_DB)
    init_triples_db(TRIPLES_DB)
    print(f"Documents DB ready: {DOCUMENTS_DB}")
    print(f"Triples DB ready:   {TRIPLES_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
