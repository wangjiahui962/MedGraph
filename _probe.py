# -*- coding: utf-8 -*-
import json, sys
from pathlib import Path
ROOT = Path(r"e:\研一\暑期作业\知识图谱构建项目\MedGraph")
records = json.loads((ROOT / "data" / "processed" / "preprocessed_documents.json").read_text(encoding="utf-8"))
targets = ["doc_000001", "doc_000003", "doc_000005", "doc_000019", "doc_000022", "doc_000027"]
for r in records:
    if r.get("document_id") in targets:
        ctrl = (r.get("content") or "").replace("\n", " ")
        print(f"=== {r['document_id']}  title={r.get('title')!r} len={len(ctrl)} ===")
        print("  content:", ctrl[:300])
        print()