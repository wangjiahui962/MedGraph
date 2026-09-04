# -*- coding: utf-8 -*-
"""按（主=subject、关系=relation、客=object）三者去重：完全相同的三元组只保留一条。

即“主、客、关系都重复”的数据才删；同一对主客但关系不同（如 HAS_SYMPTOM 与 MAY_CAUSE）各自保留。

DB 用法（作用于 data/triples.db）：
    python db/dedupe_triples.py            # 预览将被清理的行（dry-run，不删除）
    python db/dedupe_triples.py --apply    # 执行删除（先自动备份 triples.db.bak_*）

JSON 用法（数组，元素含 subject/relation/object 字段）：
    python db/dedupe_triples.py --json data/processed/triples_extracted_xxx.json
    python db/dedupe_triples.py --json data/processed/triples_extracted_xxx.json --apply

可选 --key：
    subject,relation,object  （默认）主客关系全同才去重
    subject,object           （旧规则）只要主客相同就去重，关系不同的也会被并掉

保留策略（同一组里挑一条）：
    1. confidence 更高者优先；
    2. 仍并列则保留更早写入（DB 里 id 更小 / JSON 里更靠前）的一行。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRIPLES_DB = ROOT / "data" / "triples.db"

_DB_COLUMNS = [
    "id", "subject", "subject_type", "relation",
    "object", "object_type", "source_document_id",
    "source_text", "confidence", "layer", "trained",
]

_KEY_CHOICES = {
    "subject,relation,object": ("subject", "relation", "object"),
    "subject,object": ("subject", "object"),
}
_DEFAULT_KEY = "subject,relation,object"


def _key(row: dict[str, Any], key_fields: tuple[str, ...]) -> tuple[str, ...]:
    """按 key_fields 生成去重键，忽略各字段首尾空白。"""
    return tuple(str(row.get(f) or "").strip() for f in key_fields)


def _keep_order(row: dict[str, Any]) -> int:
    """原始写入顺序：DB 用自增 id，JSON 用列表下标。"""
    return int(row.get("id", row.get("_index", 0)))


def _rank(row: dict[str, Any]) -> tuple[float, int]:
    """排序键：置信度越高、写入越早的排越前，第一条作为保留对象。"""
    return (-float(row.get("confidence") or 0.0), _keep_order(row))


def _load_db_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(zip(_DB_COLUMNS, r))
        for r in conn.execute(f"SELECT {', '.join(_DB_COLUMNS)} FROM triples")
    ]


def plan_dedup(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (保留行, 待删除行)：同一 key 只保留 _rank 最优的一条。"""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_key(row, key_fields), []).append(row)

    keep: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    for group in groups.values():
        if len(group) < 2:
            keep.extend(group)
            continue
        group.sort(key=_rank)
        keep.append(group[0])
        losers.extend(group[1:])
    return keep, losers


def _describe(row: dict[str, Any], key_fields: tuple[str, ...]) -> str:
    if "relation" in key_fields:
        label = f"{row.get('subject')} --{row.get('relation')}--> {row.get('object')}"
    else:
        label = f"{row.get('subject')} <- {row.get('object')}"
    return (
        f"[{row.get('id', row.get('_index'))}] {label} "
        f"(type={row.get('object_type')}, conf={row.get('confidence')}, doc={row.get('source_document_id')})"
    )


def _print_plan(rows: list[dict[str, Any]], losers: list[dict[str, Any]], key_fields: tuple[str, ...]) -> None:
    loser_ids = {id(row) for row in losers}
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_key(row, key_fields), []).append(row)
    for _k, group in grouped.items():
        if len(group) < 2:
            continue
        first = _describe(group[0], key_fields)
        print(f"重复组 {first}：共 {len(group)} 条")
        for row in group:
            mark = "保留" if id(row) not in loser_ids else "删除"
            print(f"  [{mark}] {_describe(row, key_fields)}")


def dedupe_db(apply: bool, key_fields: tuple[str, ...]) -> int:
    conn = sqlite3.connect(TRIPLES_DB)
    try:
        rows = _load_db_rows(conn)
        keep, losers = plan_dedup(rows, key_fields)
        if not losers:
            print(f"没有需要去重的行（key={','.join(key_fields)}）。")
            return 0
        print(f"发现 {len(losers)} 行重复（key={','.join(key_fields)}）：")
        _print_plan(rows, losers, key_fields)
        if not apply:
            print("\n[预览模式] 未做任何修改。确认后执行：python db/dedupe_triples.py --apply")
            return 0
        backup = TRIPLES_DB.with_name(
            f"triples.db.bak_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        shutil.copy2(TRIPLES_DB, backup)
        loser_ids = [int(r["id"]) for r in losers]
        conn.execute("BEGIN")
        conn.executemany("DELETE FROM triples WHERE id = ?", [(i,) for i in loser_ids])
        conn.commit()
        print(f"\n已备份到 {backup}")
        print(f"已删除 {len(loser_ids)} 行，保留 {len(keep)} 行，"
              f"当前库内共 {conn.execute('SELECT COUNT(*) FROM triples').fetchone()[0]} 行。")
        return 0
    finally:
        conn.close()


def dedupe_json(path: Path, apply: bool, key_fields: tuple[str, ...]) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path} 顶层必须是 JSON 数组")
    for index, item in enumerate(data):
        item.setdefault("_index", index)
    keep, losers = plan_dedup(data, key_fields)
    if not losers:
        print(f"{path}: 没有需要去重的行（key={','.join(key_fields)}）。")
        return 0
    print(f"{path}: 发现 {len(losers)} 行重复：")
    _print_plan(data, losers, key_fields)
    if not apply:
        print("\n[预览模式] 未写回文件。确认后执行：--apply")
        return 0
    for row in keep:
        row.pop("_index", None)
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(
        json.dumps(keep, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n已备份到 {backup}")
    print(f"已写回 {path}：{len(data)} 条 -> {len(keep)} 条。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="三元组去重：默认主、客、关系三者都相同才去重（完全重复只保留一条）。"
    )
    ap.add_argument("--apply", action="store_true", help="真正执行删除/写回（默认仅预览）")
    ap.add_argument("--json", type=Path, help="改为对指定三元组 JSON 数组去重，而不是 data/triples.db")
    ap.add_argument("--key", choices=list(_KEY_CHOICES), default=_DEFAULT_KEY,
                    help="去重键，默认 subject,relation,object；旧规则可选 subject,object")
    args = ap.parse_args()
    key_fields = _KEY_CHOICES[args.key]
    if args.json:
        return dedupe_json(args.json, args.apply, key_fields)
    return dedupe_db(args.apply, key_fields)


if __name__ == "__main__":
    raise SystemExit(main())
