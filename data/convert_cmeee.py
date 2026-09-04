# -*- coding: utf-8 -*-
"""把 CMeEE 语料转成 char 级 BIO 标注，供深度学习层 NER 训练使用。

CMeEE 原文格式（每行一条 JSON）：
    {"text": "……", "entities": [{"start_idx": 3, "end_idx": 7, "type": "pro", "entity": "房室结消融"}]}

本项目 NER 标签体系（见 extraction/deep_learning_layer.py 的 ENTITY_TYPES_FOR_NER）：
    Disease / Symptom / Drug / Treatment / Examination / RiskFactor

CMeEE 的 9 类实体 -> 本项目实体映射：
    dis -> Disease        部位 bod、科室 dep、微生物 mic 不纳入本项目本体，直接丢弃
    sym -> Symptom
    dru -> Drug
    pro -> Treatment
    equ / ite -> Examination
    （RiskFactor 无对应，继续由规则层弱监督词典提供）

输出：data/processed/ner_cmeee_labels.json，结构同 ner_weak_labels.json：
    [{"sentence": 文本, "labels": [char 级 BIO 标签]}, ...]

运行：python data/convert_cmeee.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
CME_DIR = DATA_DIR / "external" / "cmeee"
OUT = DATA_DIR / "processed" / "ner_cmeee_labels.json"

# CMeEE 类型 -> 本项目实体类型（未列出的类型丢弃）
CME_TYPE_MAP: dict[str, str] = {
    "dis": "Disease",
    "sym": "Symptom",
    "dru": "Drug",
    "pro": "Treatment",
    "equ": "Examination",
    "ite": "Examination",
}

# 超过 BERT 位置上限的文本过滤掉（避免训练时超长）
MAX_CHARS = 500


def char_labels_of(text: str, entities: list[dict]) -> list[str]:
    """把一条文本的 CMeEE 实体转成 char 级 BIO 标签。"""
    labels = ["O"] * len(text)
    for ent in entities:
        etype = CME_TYPE_MAP.get(ent.get("type"))
        if etype is None:
            continue
        start, end = ent.get("start_idx", 0), ent.get("end_idx", 0)
        # CMeEE 的 end_idx 为闭区间（含末字符），转成切片习惯：右开 +1
        end = end + 1
        if start < 0 or end > len(text) or start >= end:
            continue
        # 与已标注实体重叠时跳过，避免破坏 BIO 结构（CMeEE 基本无重叠，此处仅兜底）
        if labels[start] != "O":
            continue
        labels[start] = f"B-{etype}"
        for i in range(start + 1, end):
            labels[i] = f"I-{etype}"
    return labels


def convert() -> int:
    samples: list[dict] = []
    type_counter: Counter = Counter()
    skipped_long = 0

    for name in ("CMeEE_train.json", "CMeEE_dev.json"):
        path = CME_DIR / name
        if not path.is_file():
            print(f"WARN: 缺少 {path}，请先运行 download_cmeee.py", file=sys.stderr)
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        for rec in records:
            text = rec.get("text") or ""
            entities = rec.get("entities") or []
            if not text or not entities:
                continue
            if len(text) > MAX_CHARS:
                skipped_long += 1
                continue
            labels = char_labels_of(text, entities)
            # 至少含一个被纳入本体的实体才保留
            if not any(lbl != "O" for lbl in labels):
                continue
            samples.append({"sentence": text, "labels": labels})
            for lbl in labels:
                if lbl != "O":
                    type_counter[lbl[2:]] += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"CMeEE 转换完成：{len(samples)} 条含实体句子 -> {OUT}")
    print(f"实体类型数量：{dict(type_counter)}")
    print(f"超长（>{MAX_CHARS}字）已过滤：{skipped_long}")
    return 0


if __name__ == "__main__":
    raise SystemExit(convert())