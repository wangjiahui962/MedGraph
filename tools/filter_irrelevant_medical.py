#!/usr/bin/env python3
"""Remove clearly off-topic records from the local medical corpus and derived graphs.

The title rules are intentionally conservative: only unambiguous non-medical
topics (politics, entertainment, sports, unrelated publications, etc.) are
removed. Ambiguous biomedical terms remain for later manual review.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/documents.db"
TRIPLE_DB = ROOT / "data/triples.db"
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP = ROOT / f".data-backup-{STAMP}"

PATTERN = re.compile(
    r"法轮功|法輪功|共产党|共產黨|军训|體育教育|体育教育|作家文摘|中国外汇|當代貴州|当代贵州|中国审计|中国科学·化学|光学学报|中国水稻科学|国家科学评论|国防大学霸凌致死案|"
    r"BLEACH角色列表|HUNTER×HUNTER|哆啦A夢|哆啦A梦|乐队|歌曲|电影|电视剧|作家|诗人|运动员|皇帝|生物学家|花样滑冰|大奖赛|赛道|地震|火灾|踩踏事故|"
    r"漫长的告别|联合国儿童基金会|任內逝世的國家元首與政府首腦列表|適應性體育活動|适应性体育活动|"
    r"孙春兰|高君宇|阿维马埃尔·古斯曼|毛泽东|康生|蒋经国|张桂梅|向秀丽|阿不来提·阿不都热西提|趙燕俠|宋宜山|司马义·艾买提|司马义·铁力瓦尔地"
)


def backup(path: Path) -> None:
    if path.exists():
        target = BACKUP / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main() -> None:
    BACKUP.mkdir(parents=True, exist_ok=True)
    backup(DB)
    backup(TRIPLE_DB)
    for path in (ROOT / "data/processed").glob("*.json"):
        backup(path)
    for path in (ROOT / "frontend/public/data").glob("*.json"):
        backup(path)

    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT document_id, title FROM documents").fetchall()
    removed = [(doc_id, title) for doc_id, title in rows if PATTERN.search(title or "")]
    ids = {doc_id for doc_id, _ in removed}
    conn.executemany("DELETE FROM documents WHERE document_id = ?", [(x,) for x in ids])
    conn.executemany("DELETE FROM extract_state WHERE document_id = ?", [(x,) for x in ids])
    conn.commit()
    conn.close()

    def filter_json(path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(value, list):
            filtered = [
                row for row in value
                if not (isinstance(row, dict) and row.get("source_document_id") in ids)
                and not (isinstance(row, dict) and row.get("document_id") in ids)
            ]
            if len(filtered) != len(value):
                path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")

    for path in (ROOT / "data/processed").glob("*.json"):
        filter_json(path)
    filter_json(ROOT / "data/raw/medical_sample.json")
    for path in (ROOT / "frontend/public/data").glob("*.json"):
        filter_json(path)

    if TRIPLE_DB.exists():
        conn = sqlite3.connect(TRIPLE_DB)
        conn.executemany("DELETE FROM triple_evidence WHERE source_document_id = ?", [(x,) for x in ids])
        conn.executemany("DELETE FROM triples WHERE source_document_id = ?", [(x,) for x in ids])
        conn.commit()
        conn.close()

    report = ROOT / "data/processed/irrelevant_data_report.json"
    previous = []
    if report.exists():
        try:
            previous = json.loads(report.read_text(encoding="utf-8")).get("removed", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    merged = {x["document_id"]: x for x in previous if isinstance(x, dict) and x.get("document_id")}
    merged.update({i: {"document_id": i, "title": t} for i, t in removed})
    report.write_text(json.dumps({"removed_count": len(merged), "removed": list(merged.values()), "backup": str(BACKUP)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"removed={len(removed)} cumulative={len(merged)} backup={BACKUP}")
    for doc_id, title in removed:
        print(f"{doc_id}\t{title}")


if __name__ == "__main__":
    main()
