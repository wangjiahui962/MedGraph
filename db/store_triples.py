#!/usr/bin/env python3
"""【三元组入库 / 导出】将信息抽取得到的三元组写入 data/triples.db，并支持导出给前端。

1. 读取抽取模块的输出 JSON（默认 data/processed/triples_extracted.json，
   即 extraction/extract.py 的输出；也兼容 data/processed/triples.json）。
2. 逐条插入到 data/triples.db 的 triples 表，字段与抽取结果键一一对应：
       subject / subject_type / relation / object / object_type /
       source_document_id / source_text / confidence / layer
   trained 字段（该三元组参与模型训练的次数）不在抽取结果里，插入时走默认值 0，
   由 dl_train.py 训练完成后按 source_ids 关联 +1。
3. 去重策略：以 (subject, relation, object, object_type) 唯一索引去重，
   重复运行本脚本不会产生重复三元组（INSERT OR IGNORE）。
4. 提供查询接口 query_triples()，支持按 subject / relation / object / source_document_id 检索。
5. 提供 export_triples_json()：把库里最新结果导出为前端约定的 data/processed/triples.json
   （React 工作台通过 predev/prebuild 自动同步到 public/data/），DB 即数据源。

运行：
    python db/store_triples.py               # 默认：导入抽取结果到 DB
    python db/store_triples.py --export      # 导出 DB -> data/processed/triples.json
    python db/store_triples.py 文件.json     # 导入指定 JSON 到 DB
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRIPLES_DB = ROOT / "data" / "triples.db"
# 抽取模块统一输出文件（extraction/extract.py 写到此处）
DEFAULT_INPUT = ROOT / "data" / "processed" / "triples_extracted.json"
# 前端约定的三元组文件（frontend predev/prebuild 会同步到 public/data/triples.json）
DEFAULT_EXPORT = ROOT / "data" / "processed" / "triples.json"

# JSON 三元组 -> 库表字段（含抽取层与置信度，便于按层统计效果）
DB_FIELDS = (
    "subject", "subject_type", "relation", "object", "object_type",
    "source_document_id", "source_text", "confidence", "layer",
)
# 导出给前端时只保留前端契约要求的 7 个字段（见 frontend/README.md）
EXPORT_FIELDS = DB_FIELDS[:7]


def load_triples(path: Path) -> list[dict[str, Any]]:
    """读取抽取输出的三元组 JSON（应为列表），缺失/损坏时返回空列表并告警。"""
    if not path.is_file():
        print(f"WARN: 未找到抽取结果文件 {path}，请先运行 extraction/extract.py", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARN: {path} 不是合法 JSON（{exc}）", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def _table() -> None:
    """确保 triples 表存在（复用 db/init_db.py 的建表逻辑，幂等）。"""
    from db import init_db
    init_db.init_triples_db(TRIPLES_DB)


def clear_triples() -> int:
    """清空三元组库：删除 triples 表并按最新结构重建，返回清空前的条数。

    用于重跑实验前清空旧数据，避免与新抽取结果混淆；重建表确保包含
    confidence / layer 字段（旧表可能没有这些列）。
    """
    before = 0
    with sqlite3.connect(TRIPLES_DB) as conn:
        try:
            before = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        except sqlite3.OperationalError:
            pass  # 表不存在，视为空库
        conn.execute("DROP TABLE IF EXISTS triples")
        conn.commit()
    _table()
    return before


def insert_triples(conn: sqlite3.Connection, triples: list[dict[str, Any]]) -> int:
    """逐条插入三元组，返回实际新增条数（基于唯一索引去重）。"""
    sql = (
        "INSERT OR IGNORE INTO triples "
        "(subject, subject_type, relation, object, object_type, source_document_id, source_text, confidence, layer) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    added = 0
    for t in triples:
        if not isinstance(t, dict):
            continue
        subject, relation_, object_ = t.get("subject"), t.get("relation"), t.get("object")
        if not subject or not relation_ or not object_:
            # 缺少主语/关系/宾语 视为无效三元组
            continue
        # confidence 缺失时按层给默认值，layer 缺失标 unknown
        values = tuple(t.get(f) for f in DB_FIELDS[:7]) + (
            t.get("confidence", 0.0), t.get("layer", "unknown"),
        )
        cursor = conn.execute(sql, values)
        triple_id = conn.execute(
            "SELECT id FROM triples WHERE subject=? AND relation=? AND object=? AND object_type=?",
            (subject, relation_, object_, t.get("object_type")),
        ).fetchone()
        if triple_id:
            conn.execute(
                "INSERT OR IGNORE INTO triple_evidence (triple_id, source_document_id, source_text) VALUES (?, ?, ?)",
                (triple_id[0], t.get("source_document_id"), t.get("source_text")),
            )
        if cursor.rowcount == 1:
            added += 1
    return added


def query_triples(
    conn: sqlite3.Connection,
    subject: str | None = None,
    relation: str | None = None,
    object: str | None = None,
    source_document_id: str | None = None,
) -> list[tuple[Any, ...]]:
    """按条件检索三元组（任一条件，未传则忽略该条件）。"""
    clauses: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("subject", subject), ("relation", relation),
        ("object", object), ("source_document_id", source_document_id),
    ):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT * FROM triples"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return conn.execute(sql, params).fetchall()


def export_triples_json(out_path: Path = DEFAULT_EXPORT) -> int:
    """把 DB 里的三元组导出为前端约定的 JSON 数组（data/processed/triples.json）。

    只导出前端契约要求的 7 个字段（EXPORT_FIELDS），保持与 frontend/README.md 一致；
    confidence / layer 留存在 DB，需要时前端可另行扩展。返回导出条数。
    """
    with sqlite3.connect(TRIPLES_DB) as conn:
        rows = conn.execute(
            f"SELECT t.id, {', '.join('t.' + f for f in EXPORT_FIELDS)} FROM triples t"
        ).fetchall()
        evidence = conn.execute(
            "SELECT triple_id, source_document_id, source_text FROM triple_evidence ORDER BY id"
        ).fetchall()
    evidence_by_triple: dict[int, list[tuple[Any, Any]]] = {}
    for triple_id, doc_id, text in evidence:
        evidence_by_triple.setdefault(triple_id, []).append((doc_id, text))
    records: list[dict[str, Any]] = []
    for row in rows:
        triple_id, *values = row
        evs = evidence_by_triple.get(triple_id) or [(values[5], values[6])]
        for doc_id, source_text in evs:
            item = dict(zip(EXPORT_FIELDS, values))
            item["source_document_id"] = doc_id or item["source_document_id"]
            item["source_text"] = source_text or item["source_text"]
            records.append(item)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已导出 {len(records)} 条三元组 -> {out_path.relative_to(ROOT)}")
    return len(records)


def main() -> int:
    # --export：直接导出 DB，不经过导入
    if len(sys.argv) > 1 and sys.argv[1] == "--export":
        _table()
        export_triples_json()
        return 0

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not path.is_file():
        print(f"WARN: 未找到抽取结果文件 {path}，请先运行 extraction/extract.py", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"ERROR: 无法解析抽取结果文件 {path}: {exc}", file=sys.stderr)
        return 1
    triples = data if isinstance(data, list) else []
    if not triples:
        # 增量抽取“本轮没有新增文档”时结果是空数组，属正常情况，不算失败
        print("结果为空：本轮没有新增三元组，跳过入库。")
        return 0

    _table()
    with sqlite3.connect(TRIPLES_DB) as conn:
        added = insert_triples(conn, triples)
        total = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        conn.commit()

    print(f"来源：{path}")
    print(f"本次读取 {len(triples)} 条，新增入库 {added} 条，库里现有 {total} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
