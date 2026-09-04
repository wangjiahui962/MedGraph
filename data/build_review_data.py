# -*- coding: utf-8 -*-
"""【主动学习闭环 · 人工审查数据工具】

流程：抽取出的三元组 → 人工审查 → 反馈训练
    1. export  : 把 triples.db 导出为可人工编辑的审查清单 review_list.json
    2. convert : 读回已标记的审查清单，把"保留"的三元组用证据句定位实体，
                 转成 char 级 BIO 标注，输出 ner_review_labels.json，
                 供 dl_train.py 合并（--resume）重训，形成"抽取→审查→学习"闭环。

审查清单每条记录（review 字段由人工填写）：
    null    —— 未审查（默认值）
    "keep"  —— 保留：可直接把三元组作为训练监督
    "reject"—— 删除：不进入训练（也不进图谱）
    修正方式：直接改 subject / relation / object / 类型字段后再标 "keep"。

转换规则（review_to_bio）：
    - 仅 review == "keep" 且 subject/object 都能在 source_text 中精确匹配时才生成标注；
    - 只转换 NER 支持的 6 类实体（Disease/Symptom/Drug/Treatment/Examination/RiskFactor），
      其余类型（Department/Population/Complication）DL 层 NER 不学，自动跳过；
    - 长词优先匹配、已被占用字符跳过，避免破坏 BIO 结构；
    - 同一句子的多条三元组合并标注，按 (sentence, labels) 去重。

运行：
    python data/build_review_data.py export    # ① 导出审查清单 review_list.json
    # ② 人工编辑 review_list.json（改 review 字段 / 修正三元组）
    python data/build_review_data.py convert   # ③ 转 BIO -> ner_review_labels.json
    # ④ 合并训练集并增量重训：
    #    python extraction/dl_train.py --data data/processed/ner_train_labels.json --epochs 2 --resume
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TRIPLES_DB = ROOT / "data" / "triples.db"
REVIEW_LIST = ROOT / "data" / "processed" / "review_list.json"
REVIEW_LABELS = ROOT / "data" / "processed" / "ner_review_labels.json"
# 合并后的最终训练集（dl_train.py 的 MERGED_OUT）
TRAIN_SET = ROOT / "data" / "processed" / "ner_train_labels.json"

# 与 deep_learning_layer.py 保持一致：DL 层 NER 只学这 6 类，其余类型跳过
NER_ENTITY_TYPES: set[str] = {
    "Disease", "Symptom", "Drug", "Treatment", "Examination", "RiskFactor",
}


# ---------------------------------------------------------------------------
# ① 导出审查清单
# ---------------------------------------------------------------------------

def export_review_list() -> int:
    """把 triples.db 全部三元组导出为审查清单（每条带证据句与审查字段）。"""
    if not TRIPLES_DB.is_file():
        print(f"ERROR: 三元组库不存在: {TRIPLES_DB}，请先运行 extraction/extract.py + db/store_triples.py",
              file=sys.stderr)
        return 1

    with sqlite3.connect(TRIPLES_DB) as conn:
        rows = conn.execute(
            "SELECT id, subject, subject_type, relation, object, object_type, "
            "       source_document_id, source_text, confidence, layer, trained "
            "FROM triples ORDER BY id"
        ).fetchall()

    if not rows:
        print("WARN: triples 表为空，没有可审查的三元组", file=sys.stderr)
        return 0

    records = [
        {
            "id": r[0],
            "subject": r[1],
            "subject_type": r[2],
            "relation": r[3],
            "object": r[4],
            "object_type": r[5],
            "source_document_id": r[6],
            "source_text": r[7],
            "confidence": r[8],
            "layer": r[9],
            "trained": r[10],  # 已参与训练的次数，便于人工判断是否值得再喂一轮
            "review": None,  # 人工填写：null / "keep" / "reject"
        }
        for r in rows
    ]

    REVIEW_LIST.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_LIST.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已导出审查清单：{len(records)} 条 -> {REVIEW_LIST}")
    print("请人工编辑该文件：将 review 字段改为 \"keep\"（保留）或 \"reject\"（删除）；")
    print("如需修正，直接改 subject/relation/object 等字段后再标 keep。")
    return 0


# ---------------------------------------------------------------------------
# ② 审查清单 → BIO 训练数据
# ---------------------------------------------------------------------------

def _mark_entity(sentence: str, labels: list[str], term: str, etype: str) -> bool:
    """在句中标注一个实体（长词优先、跳过已占用字符），返回是否标注成功。

    labels 为当前句子的 char 级标签，会被就地修改。边界精确（B-/I- 结构）。
    """
    if not term or term not in sentence:
        return False
    # 长词优先：从最长的匹配位置开始找（此处 term 是单个词，直接 find 全词）
    idx = sentence.find(term)
    # 若该词被别的实体占用则换个位置找，避免嵌套/重叠破坏 BIO
    while idx != -1:
        if all(labels[idx + k] == "O" for k in range(len(term))):
            labels[idx] = f"B-{etype}"
            for k in range(1, len(term)):
                labels[idx + k] = f"I-{etype}"
            return True
        idx = sentence.find(term, idx + 1)
    return False


def review_to_bio() -> int:
    """读回审查清单，把 review == "keep" 的三元组转成 BIO 训练数据。

    输出 ner_review_labels.json：
    [{"sentence": str, "labels": [char级BIO...], "source_ids": [三元组id...]}, ...]
    source_ids 记录该句由哪些三元组标注而来，dl_train.py 训练完成后据此给 triples.db 的 trained +1。
    """
    if not REVIEW_LIST.is_file():
        print(f"ERROR: 审查清单不存在: {REVIEW_LIST}，请先运行 export", file=sys.stderr)
        return 1

    records = json.loads(REVIEW_LIST.read_text(encoding="utf-8"))
    kept = [r for r in records if r.get("review") == "keep"]
    rejected = [r for r in records if r.get("review") == "reject"]
    print(f"审查清单共 {len(records)} 条：keep={len(kept)}，reject={len(rejected)}，未审查={len(records) - len(kept) - len(rejected)}")

    # 按句子聚合标注：同一句多条三元组合并；
    # 同时记录每个句子由哪些三元组（triples 表的 id）标注而来，
    # 供 dl_train.py 训练完成后把对应三元组的 trained 计数 +1。
    sent_labels: dict[str, list[str]] = {}
    sent_ids: dict[str, set[int]] = {}
    skipped_type = Counter()    # 因类型不在 NER 6 类而跳过
    skipped_nomatch = Counter()  # 因实体在证据句里匹配不上而跳过
    for r in kept:
        st, ot = r.get("subject_type"), r.get("object_type")
        # 只转换 NER 支持的 6 类（Department/Population/Complication 模型不学）
        if st not in NER_ENTITY_TYPES or ot not in NER_ENTITY_TYPES:
            skipped_type[(st, ot)] += 1
            continue
        sent = (r.get("source_text") or "").strip()
        if not sent:
            skipped_nomatch["空证据句"] += 1
            continue
        labels = sent_labels.setdefault(sent, ["O"] * len(sent))
        # 长词优先标注，避免短词抢占长词片段
        contributed = False  # 该三元组是否至少标注成功一个实体（是才算参与训练）
        for term, etype in sorted(((r["subject"], st), (r["object"], ot)),
                                  key=lambda x: len(x[0]), reverse=True):
            if _mark_entity(sent, labels, term, etype):
                contributed = True
            else:
                skipped_nomatch[term] += 1
        if contributed:
            sent_ids.setdefault(sent, set()).add(int(r["id"]))

    samples = [
        {"sentence": s, "labels": lbl, "source_ids": sorted(sent_ids.get(s, ()))}
        for s, lbl in sent_labels.items()
    ]
    samples.sort(key=lambda x: x["sentence"])

    REVIEW_LABELS.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_LABELS.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 BIO 训练数据：{len(samples)} 条句子 -> {REVIEW_LABELS}")
    if skipped_type:
        print(f"跳过（类型不在 NER 6 类）：{dict(skipped_type)}")
    if skipped_nomatch:
        print(f"跳过（实体未能在证据句中匹配，仅能作图谱数据、不能作训练）：{dict(list(skipped_nomatch.items())[:5])}")
    return 0


# ---------------------------------------------------------------------------
# ③ 合并进训练集（可选快捷命令）
# ---------------------------------------------------------------------------

def merge_into_train() -> int:
    """把审查产物合并进最终训练集 ner_train_labels.json（幂等：按 sentence 去重）。"""
    if not REVIEW_LABELS.is_file():
        print(f"ERROR: 审查 BIO 数据不存在: {REVIEW_LABELS}，请先运行 convert", file=sys.stderr)
        return 1
    if not TRAIN_SET.is_file():
        print(f"ERROR: 训练集不存在: {TRAIN_SET}，请先运行 dl_train.py 的 merge_dataset", file=sys.stderr)
        return 1

    train = json.loads(TRAIN_SET.read_text(encoding="utf-8"))
    review = json.loads(REVIEW_LABELS.read_text(encoding="utf-8"))

    seen = {(s["sentence"]) for s in train}
    added = 0
    for s in review:
        if s["sentence"] not in seen:
            train.append(s)
            seen.add(s["sentence"])
            added += 1

    TRAIN_SET.write_text(json.dumps(train, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"合并完成：训练集新增 {added} 条，共 {len(train)} 条 -> {TRAIN_SET}")
    print("下一步：python extraction/dl_train.py --data data/processed/ner_train_labels.json --epochs 2 --resume")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="主动学习闭环：人工审查三元组并转训练数据")
    ap.add_argument("action", choices=["export", "convert", "merge"],
                    help="export=导出审查清单 | convert=审查清单转BIO | merge=合并进训练集")
    args = ap.parse_args()

    if args.action == "export":
        return export_review_list()
    if args.action == "convert":
        return review_to_bio()
    return merge_into_train()


if __name__ == "__main__":
    raise SystemExit(main())
