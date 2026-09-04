# -*- coding: utf-8 -*-
"""临时验证：修复后单篇路由句子数是否大幅下降。用完即删。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import deep_learning_layer as dl  # noqa: E402

recs = json.loads((ROOT / "data/processed/preprocessed_documents.json").read_text(encoding="utf-8"))

total_routed = 0
total_sents = 0
for rec in recs[:10]:
    pred = dl.predict_record(rec)
    routed = len(pred.get("llm_sentences") or [])
    n_sent = len(rec["sentences"])
    total_routed += routed
    total_sents += n_sent
    print(f"{rec['document_id']} {rec['title'][:12]:<14} 句子{n_sent:>2} | 路由 {routed:>2} | 三元组 {len(pred['triples']):>3}", flush=True)
print(f"\n前10篇合计：句子 {total_sents}，路由 {total_routed}，路由率 {total_routed / total_sents:.0%}")
