# -*- coding: utf-8 -*-
"""从 CMeEE 实体构建大规模医学词典，对项目自有文档做远程监督（精确匹配）标注。

思路：
    - CMeEE 是真人标注的金标数据，其标注出的实体（dis/sym/dru/pro/equ/ite）本身就是
      高质量医学术语；把它们去重整理成「术语 -> 实体类型」词典，规模远超规则层手写的
      几十个词。
    - 用该词典对 data/processed/preprocessed_documents.json 的句子做精确匹配，
      生成 char 级 BIO 标注（边界精确，类型取该词在 CMeEE 中出现最多的类型）。

与规则层弱监督（rule_layer 手写词典）的区别：
    - 覆盖更广：术语来自上万条金标，而非手写几十词；
    - 类型更可信：类型由真人标注的多数投票决定，而非手写归类。

输出：data/processed/ner_distant_labels.json（结构同 ner_weak_labels.json）。

运行：python data/build_distant_labels.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
CME_DIR = DATA_DIR / "external" / "cmeee"
PREPROCESSED = DATA_DIR / "processed" / "preprocessed_documents.json"
OUT = DATA_DIR / "processed" / "ner_distant_labels.json"

# 与 convert_cmeee.py 保持一致：CMeEE 类型 -> 本项目实体类型（其余类型丢弃）
CME_TYPE_MAP: dict[str, str] = {
    "dis": "Disease",
    "sym": "Symptom",
    "dru": "Drug",
    "pro": "Treatment",
    "equ": "Examination",
    "ite": "Examination",
}

# 词典过滤阈值：过短/过长/含标点的词噪声大，低频词可能是标注噪声
MIN_TERM_LEN = 2
MAX_TERM_LEN = 40
MIN_TERM_FREQ = 2
_TERM_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


def build_lexicon() -> dict[str, str]:
    """从 CMeEE 实体构建「术语 -> 实体类型」词典（类型取多数票）。"""
    term_types: dict[str, Counter] = defaultdict(Counter)
    for name in ("CMeEE_train.json", "CMeEE_dev.json"):
        path = CME_DIR / name
        if not path.is_file():
            print(f"WARN: 缺少 {path}，请先运行 download_cmeee.py", file=sys.stderr)
            continue
        for rec in json.loads(path.read_text(encoding="utf-8")):
            for ent in rec.get("entities") or []:
                etype = CME_TYPE_MAP.get(ent.get("type"))
                if etype is None:
                    continue
                term = (ent.get("entity") or "").strip()
                if not (MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN):
                    continue
                if _TERM_RE.fullmatch(term) is None:
                    continue
                term_types[term][etype] += 1

    lexicon: dict[str, str] = {}
    for term, counter in term_types.items():
        etype, cnt = counter.most_common(1)[0]
        if cnt < MIN_TERM_FREQ:
            continue
        lexicon[term] = etype
    return lexicon


def label_sentence(sentence: str, lexicon: dict[str, str]) -> list[str]:
    """按词典对单句做最长优先的精确匹配，返回 char 级 BIO 标签。"""
    labels = ["O"] * len(sentence)
    # 长词优先匹配，避免短词抢占长词的片段
    for term in sorted(lexicon, key=len, reverse=True):
        etype = lexicon[term]
        start = 0
        while True:
            idx = sentence.find(term, start)
            if idx == -1:
                break
            # 已被更长的词占用则为避免破坏 BIO 跳过；否则整体标注
            if not any(labels[idx + k] != "O" for k in range(len(term))):
                labels[idx] = f"B-{etype}"
                for k in range(1, len(term)):
                    labels[idx + k] = f"I-{etype}"
            start = idx + 1
    return labels


def main() -> int:
    if not PREPROCESSED.is_file():
        print(f"ERROR: 预处理产物不存在: {PREPROCESSED}", file=sys.stderr)
        return 1

    lexicon = build_lexicon()
    print(f"词典构建完成：{len(lexicon)} 个术语")
    type_dist = Counter(lexicon.values())
    print(f"词典类型分布：{dict(type_dist)}")

    records = json.loads(PREPROCESSED.read_text(encoding="utf-8"))
    samples: list[dict] = []
    type_char_count: Counter = Counter()
    for rec in records:
        for sent in rec.get("sentences") or []:
            if not sent:
                continue
            labels = label_sentence(sent, lexicon)
            if not any(lbl != "O" for lbl in labels):
                continue
            samples.append({"sentence": sent, "labels": labels})
            for lbl in labels:
                if lbl != "O":
                    type_char_count[lbl[2:]] += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"远程监督标注完成：{len(samples)} 条含实体句子 -> {OUT}")
    print(f"实体类型字符量：{dict(type_char_count)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())