# -*- coding: utf-8 -*-
"""测试脚本：直接从 documents.db 读取文档，抽取出“候选关系语句”，
并打印每条候选三元组对应的 证据句(source_text) / 置信度(confidence) / 抽取层级(layer)。

运行：python extraction/test_extract_db.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import deep_learning_layer, llm_layer  # noqa: E402
from extraction.extract import extract_one  # noqa: E402

DB = ROOT / "data" / "documents.db"
COLUMNS = [
    "document_id", "category_ids", "title", "content",
    "source_url", "license", "collected_at", "content_hash", "quality_score",
]


def load_documents() -> list[dict[str, Any]]:
    """读取 documents.db 的 documents 表全部记录。"""
    conn = sqlite3.connect(DB)
    cols_sql = ", ".join(COLUMNS)
    rows = conn.execute(f"SELECT {cols_sql} FROM documents").fetchall()
    conn.close()
    return [dict(zip(COLUMNS, row)) for row in rows]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="从 documents.db 抽取候选三元组并打印")
    ap.add_argument("--limit", type=int, default=0,
                    help="只抽取前 N 篇文档（按 documents.db 顺序；0=全部）")
    ap.add_argument("--random", type=int, default=0,
                    help="随机抽取 N 篇文档（与 --limit 互斥，固定随机种子可复现）")
    ap.add_argument("--seed", type=int, default=42, help="随机抽样种子（默认 42）")
    args = ap.parse_args()

    records = load_documents()
    if not records:
        print("ERROR: documents.db 中没有数据", file=sys.stderr)
        return 1

    if args.random > 0:
        import random

        random.seed(args.seed)
        records = random.sample(records, min(args.random, len(records)))
    elif args.limit > 0:
        records = records[:args.limit]

    print(f"共读取 {len(records)} 条文档；深度学习层可用={deep_learning_layer.AVAILABLE}；"
          f"LLM层可用={llm_layer.is_available()}\n")

    total_candidates = 0
    for rec in records:
        triples = extract_one(rec)
        if not triples:
            continue
        total_candidates += len(triples)
        print(f"【{rec.get('document_id')}】{rec.get('title')}（候选 {len(triples)} 条）")
        for t in triples:
            evidence = t.get("source_text") or ""
            if len(evidence) > 48:
                evidence = evidence[:48] + "…"
            print(f"    层级={t['layer']:<5} 关系={t['relation_label']}({t['relation']}) "
                  f"对象={t['object']} 置信度={t['confidence']} 证据句={evidence}")
        print()

    print(f"候选三元组合计：{total_candidates} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())