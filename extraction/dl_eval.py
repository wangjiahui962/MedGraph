# -*- coding: utf-8 -*-
"""【验证集评估】在 documents.db 抽样上评估 DL 层，并调优过滤/路由阈值。

参考"金标"（pseudo-gold）：
    用 CMeEE 实体构建的大规模医学词典（data/build_distant_labels.py 的 build_lexicon，
    数千个真人标注术语、类型取多数票），对每句做最长优先精确匹配得到 (term, type)。
    相比规则层手写几十词，该词典覆盖广、类型可信，能给出有区分度的 P/R/F1。

用途：
    1. 量化"单字过滤 + 置信度阈值"对精度的提升（重点看 P）；
    2. 选定合适的 CONFIDENT_THRESHOLD / MIN_ENTITY_LEN；
    3. 输出被路由到 LLM 的句子占比（覆盖"DL 做不好"的场景）。

运行：python extraction/dl_eval.py [采样文档数，默认 150]
"""
from __future__ import annotations

import importlib.util
import random
import sqlite3
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import deep_learning_layer as dl  # noqa: E402
from preprocess import preprocess as pp  # noqa: E402

DB = ROOT / "data" / "documents.db"


def _load_cmeee_lexicon() -> dict[str, str]:
    """加载 CMeEE 大词典（术语 -> 实体类型）。"""
    path = ROOT / "data" / "build_distant_labels.py"
    spec = importlib.util.spec_from_file_location("_bdl", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_lexicon()


def gold_spans_of(sentence: str, lexicon: dict[str, str]) -> set[tuple[str, str]]:
    """用 CMeEE 词典对单句做最长优先精确匹配，返回 (term, type) 金标集合。"""
    gold: set[tuple[str, str]] = set()
    claimed: list[bool] = [False] * len(sentence)
    for term in sorted(lexicon, key=len, reverse=True):
        etype = lexicon[term]
        start = 0
        while True:
            idx = sentence.find(term, start)
            if idx == -1:
                break
            # 未被更长词占用才认作一个实体（避免重叠）
            if not any(claimed[idx + k] for k in range(len(term))):
                gold.add((term, etype))
                for k in range(len(term)):
                    claimed[idx + k] = True
            start = idx + 1
    return gold


def eval_thresholds(
    records: list[tuple[str, str, str]],
    lexicon: dict[str, str],
    min_lens: tuple[int, ...],
    thresholds: tuple[float, ...],
) -> None:
    print(f"CMeEE 词典规模：{len(lexicon)} 个术语")

    tokenizer, model = dl._get_model()

    sentences: list[str] = []
    gold_sets: list[set[tuple[str, str]]] = []
    for _, _, content in records:
        for sent in pp.split_sentences(pp.clean_text(content))[:8]:
            sentences.append(sent)
            gold_sets.append(gold_spans_of(sent, lexicon))

    total_gold = sum(len(g) for g in gold_sets)
    print(f"评估句数：{len(sentences)}，金标实体总数：{total_gold}\n")

    raw_spans = [dl._predict_sentence(tokenizer, model, s) for s in sentences]

    header = f"{'min_len':<8}{'conf':<8}{'P':>8}{'R':>8}{'F1':>8}{'保留实体':>10}{'路由句占比':>10}"
    print(header)
    print("-" * len(header))
    for min_len, thr in product(min_lens, thresholds):
        tp = fp = fn = 0
        kept = 0
        routed_total = 0  # 含金标且需路由的句子数
        routed_any = 0    # 含碎片/低置信的句子总数
        for spans, gold in zip(raw_spans, gold_sets):
            confident = [(t, e, c) for t, e, c in spans
                         if len(t) >= min_len and c >= thr]
            kept += len(confident)
            has_uncertain = any(len(t) < min_len or c < thr for t, e, c in spans)
            if has_uncertain:
                routed_any += 1
                if gold:
                    routed_total += 1
            pred = {(t, e) for t, e, _ in confident}
            tp += len(pred & gold)
            fp += len(pred - gold)
            fn += len(gold - pred)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        routed_ratio = routed_total / max(total_gold, 1)
        print(f"{min_len:<8}{thr:<8.2f}{p:>8.4f}{r:>8.4f}{f1:>8.4f}{kept:>10}{routed_ratio:>9.1%}")
    print(f"\n说明：任一句子出现碎片/低置信即路由到 LLM 的句子比例："
          f"{routed_any / max(len(gold_sets), 1):.1%}（全部句子口径）")


def main() -> int:
    n_docs = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT document_id, title, content FROM documents ORDER BY RANDOM() LIMIT ?",
        (n_docs,),
    ).fetchall()
    conn.close()
    if not rows:
        print("ERROR: documents.db 为空，请先运行 db/store_documents.py", file=sys.stderr)
        return 1

    random.seed(0)
    lexicon = _load_cmeee_lexicon()
    eval_thresholds(rows, lexicon, min_lens=(1, 2), thresholds=(0.5, 0.6, 0.7, 0.8))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())