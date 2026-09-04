# -*- coding: utf-8 -*-
"""【第三阶段 · 深度学习层训练脚本（弱监督 NER）】

训练一个中文医学实体 NER 模型（bert-base-chinese + TokenClassification），
用于修复规则层“后缀模板”造成的实体边界错误。

核心思路 —— 弱监督生成标注数据：
    规则层高置信（置信度 0.9）的**词典命中**结果，其实体边界准确（词典词就是精确边界），
    可直接自动转换成 BIO 标注，无需人工从零标注。训练后模型能学到“正确边界”，
    从而修正规则层那些低置信（0.7 模板）吞字/错切的坏结果。

流程：
    1. build_dataset():   读预处理文档 + 规则层词典命中 -> 生成 char 级 BIO -> 落盘
                          （本步骤不依赖 torch，可单独运行验证标注质量）
    2. train():            用上面数据微调 bert-base-chinese，处理样本不平衡，保存到 models/ner
    3. evaluate():         span 级 P/R/F1 简单评测

环境依赖（仅训练需要）：pip install torch transformers
运行：python extraction/dl_train.py
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import rule_layer  # noqa: E402
from extraction.deep_learning_layer import (  # noqa: E402
    ID2LABEL,
    LABEL2ID,
    LABELS,
    MODEL_DIR,
    ENTITY_TYPES_FOR_NER,
    _disable_incompatible_torchvision,
)

PREPROCESSED = ROOT / "data" / "processed" / "preprocessed_documents.json"
DATASET_OUT = ROOT / "data" / "processed" / "ner_weak_labels.json"
# CMeEE 语料转出的 BIO 标注（由 data/convert_cmeee.py 生成，含 Disease 等 6 类）
CME_LABELS = ROOT / "data" / "processed" / "ner_cmeee_labels.json"
# CMeEE 实体做大词典的远程监督标注（由 data/build_distant_labels.py 生成）
DISTANT_LABELS = ROOT / "data" / "processed" / "ner_distant_labels.json"
# 合并后的最终训练集（弱监督 + CMeEE + 远程监督 + 人工审查）
MERGED_OUT = ROOT / "data" / "processed" / "ner_train_labels.json"
# 本地预训练权重目录（已从 ModelScope 下载）；不存在时回退到按模型名在线加载
PRETRAINED_DIR = ROOT / "models" / "pretrained_bert"
# 三元组库：训练完成后给参与训练的三元组（source_ids 关联）的 trained 计数 +1
TRIPLES_DB = ROOT / "data" / "triples.db"


def _pretrained_source() -> str:
    """优先用本地已下载的预训练权重，避免运行时联网。"""
    return str(PRETRAINED_DIR) if PRETRAINED_DIR.is_dir() else "bert-base-chinese"

# 客体实体类型 -> 词典（取自规则层的高置信词典命中）
TYPE_TERMS: dict[str, set[str]] = {
    "Symptom": rule_layer.SYMPTOM_TERMS,
    "Drug": rule_layer.DRUG_TERMS,
    "Treatment": rule_layer.TREATMENT_TERMS,
    "Examination": rule_layer.EXAMINATION_TERMS,
    "RiskFactor": rule_layer.RISK_FACTOR_TERMS,
}


def _sentence_char_labels(sentence: str) -> list[str]:
    """为单句生成 char 级 BIO 标签（基于规则层词典命中，边界精确）。"""
    labels = ["O"] * len(sentence)
    for entity_type, terms in TYPE_TERMS.items():
        for term in terms:
            start = 0
            while True:
                idx = sentence.find(term, start)
                if idx == -1:
                    break
                # 仅标注尚未被其它实体占用的字符，避免重叠冲突
                if labels[idx] == "O":
                    labels[idx] = f"B-{entity_type}"
                    for k in range(idx + 1, idx + len(term)):
                        if labels[k] == "O":
                            labels[k] = f"I-{entity_type}"
                start = idx + max(1, len(term))
    return labels


def build_dataset() -> int:
    """生成弱监督 BIO 标注数据集（无需 torch），返回含实体句子数。"""
    if not PREPROCESSED.is_file():
        print(f"ERROR: 预处理产物不存在: {PREPROCESSED}", file=sys.stderr)
        return 0

    records = json.loads(PREPROCESSED.read_text(encoding="utf-8"))
    samples: list[dict[str, Any]] = []
    for rec in records:
        sentences = rec.get("sentences") or []
        for sent in sentences:
            if not sent:
                continue
            char_labels = _sentence_char_labels(sent)
            # 只保留至少含一个实体的句子，缓解 O 类别占比过高
            if any(lbl != "O" for lbl in char_labels):
                samples.append({"sentence": sent, "labels": char_labels})

    DATASET_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATASET_OUT.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"弱监督标注完成：{len(samples)} 条含实体句子 -> {DATASET_OUT}")
    return len(samples)


def merge_dataset() -> int:
    """合并弱监督 + CMeEE + 远程监督三种标注，生成最终训练集 MERGED_OUT。

    CMeEE 提供大规模真实标注（含 Disease/Symptom/Drug/Treatment/Examination），
    弱监督标注补充 RiskFactor（CMeEE 无此类型），
    远程监督（CMeEE 实体大词典匹配项目文档）补充本项目的领域语料覆盖。
    """
    samples: list[dict[str, Any]] = []
    if DATASET_OUT.is_file():
        samples += json.loads(DATASET_OUT.read_text(encoding="utf-8"))
        print(f"已加载弱监督标注：{len(samples)} 条")
    if CME_LABELS.is_file():
        cmeee = json.loads(CME_LABELS.read_text(encoding="utf-8"))
        print(f"已加载 CMeEE 标注：{len(cmeee)} 条")
        samples += cmeee
    else:
        print(f"WARN: CMeEE 标注不存在（{CME_LABELS}），请先运行 data/convert_cmeee.py", file=sys.stderr)
    if DISTANT_LABELS.is_file():
        distant = json.loads(DISTANT_LABELS.read_text(encoding="utf-8"))
        print(f"已加载远程监督标注：{len(distant)} 条")
        samples += distant

    if not samples:
        print("ERROR: 无任何训练数据", file=sys.stderr)
        return 0

    random.seed(0)
    random.shuffle(samples)
    MERGED_OUT.parent.mkdir(parents=True, exist_ok=True)
    MERGED_OUT.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"合并完成：共 {len(samples)} 条训练样本 -> {MERGED_OUT}")
    return len(samples)


def _bump_trained_ids(samples: list[dict[str, Any]]) -> int:
    """把参与本轮训练的审查三元组在 triples.db 中的 trained 计数 +1。

    仅当训练数据带 source_ids（build_review_data.py 的 convert 步骤写入，标识
    该句由哪些三元组标注而来）时才递增；纯 CMeEE / 弱监督 / 远程监督样本没有
    source_ids，不动数据库。返回实际递增的三元组条数。
    """
    ids: set[int] = set()
    for s in samples:
        for sid in s.get("source_ids") or []:
            try:
                ids.add(int(sid))
            except (TypeError, ValueError):
                continue
    if not ids:
        return 0
    if not TRIPLES_DB.is_file():
        print(f"WARN: triples.db 不存在，跳过 trained 计数更新: {TRIPLES_DB}", file=sys.stderr)
        return 0
    from db import init_db  # 懒加载，避免 extraction 包内循环依赖
    init_db.init_triples_db(TRIPLES_DB)  # 确保表结构与 trained 列存在（含旧库迁移）
    with sqlite3.connect(TRIPLES_DB) as conn:
        conn.executemany("UPDATE triples SET trained = trained + 1 WHERE id = ?",
                         [(i,) for i in sorted(ids)])
        conn.commit()
    print(f"已为 {len(ids)} 条参与训练的三元组 trained 计数 +1")
    return len(ids)


# ---------------------------------------------------------------------------
# 训练（需要 torch + transformers）
# ---------------------------------------------------------------------------

def _tokenize_align(tokenizer, sentence: str, char_labels: list[str]) -> tuple[list[int], list[int]]:
    """把 char 级 BIO 对齐到 token 级；特殊 token 用 -100（loss 忽略）。"""
    enc = tokenizer(sentence, return_offsets_mapping=True, add_special_tokens=True)
    input_ids: list[int] = enc["input_ids"]
    offsets = enc["offset_mapping"]
    token_labels: list[int] = []

    for start, end in offsets:
        if start == end:  # [CLS]/[SEP]
            token_labels.append(-100)
            continue
        seg_label = char_labels[start]
        if seg_label == "O":
            token_labels.append(LABEL2ID["O"])
            continue
        entity_type = seg_label[2:]
        prev_label = char_labels[start - 1] if start > 0 else "O"
        is_begin = prev_label == "O" or prev_label[2:] != entity_type
        token_labels.append(LABEL2ID[("B-" if is_begin else "I-") + entity_type])

    return input_ids, token_labels


def _span_f1(gold: list[list[tuple[int, int, str]]], pred: list[list[tuple[int, int, str]]]) -> tuple[float, float, float]:
    """按 (start, end, type) 计算 span 级 P/R/F1。"""
    def to_set(spans):
        return set(spans)

    tp = fp = fn = 0
    for g, p in zip(gold, pred):
        gs, ps = to_set(g), to_set(p)
        tp += len(gs & ps)
        fp += len(ps - gs)
        fn += len(gs - ps)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _decode_labels(ids: list[int]) -> list[tuple[int, int, str]]:
    """把 token 级标签解码为 (start, end, type) 跨度（token 粒度近似）。"""
    spans = []
    i = 0
    while i < len(ids):
        label = ID2LABEL.get(ids[i], "O")
        if label.startswith("B-"):
            entity_type = label[2:]
            j = i + 1
            while j < len(ids):
                next_label = ID2LABEL.get(ids[j], "O")
                if next_label.startswith("I-") and next_label[2:] == entity_type:
                    j += 1
                else:
                    break
            spans.append((i, j, entity_type))
            i = j
        else:
            i += 1
    return spans


def train(data_path: Path = MERGED_OUT, epochs: int = 3, resume: bool = False) -> int:
    """微调 bert-base-chinese NER 模型并保存到 models/ner。

    data_path: 训练数据 BIO 文件（默认合并集 MERGED_OUT，可传入 CME_LABELS 等单独来源）
    epochs:    训练轮数（默认 3）
    resume:    是否从 models/ner 已有权重继续训练（增量微调）。
               为 True 且 models/ner 存在时，加载已有权重作为起点并使用较小学习率，
               避免破坏之前几轮已学到的医学实体知识。
    """
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import DataLoader
        from transformers import AutoModelForTokenClassification, AutoTokenizer
        from torch.optim import AdamW
    except ImportError as exc:
        print(f"SKIP 训练：缺少 torch/transformers 依赖（{exc}）", file=sys.stderr)
        print("请先安装：pip install torch transformers", file=sys.stderr)
        return 1

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"训练设备：{device}"
          + (f"（{torch.cuda.get_device_name(0)}）" if device.type == "cuda" else "（未检测到 GPU，回退 CPU）"))

    if not data_path.is_file():
        print(f"ERROR: 训练集不存在: {data_path}", file=sys.stderr)
        print("请先运行 build_dataset() / merge_dataset() / data/convert_cmeee.py", file=sys.stderr)
        return 2

    samples = json.loads(data_path.read_text(encoding="utf-8"))
    # 起点权重：resume 时优先加载 models/ner 已有权重（增量微调），否则用预训练权重
    if resume and MODEL_DIR.is_dir():
        source = str(MODEL_DIR)
        lr = 1e-5  # 增量微调用小学习率，避免遗忘已学特征
        print(f"继续训练：从已有模型 {MODEL_DIR} 加载权重（lr={lr}）")
    else:
        source = _pretrained_source()
        lr = 2e-5
    print(f"使用预训练权重：{source}")
    tokenizer = AutoTokenizer.from_pretrained(source)

    tokenized = [_tokenize_align(tokenizer, s["sentence"], s["labels"]) for s in samples]

    # 8:1:1 切分 train/val/test
    random.seed(0)
    idxs = list(range(len(tokenized)))
    random.shuffle(idxs)
    n = len(tokenized)
    train_idx = idxs[: int(n * 0.8)]
    val_idx = idxs[int(n * 0.8): int(n * 0.9)]
    test_idx = idxs[int(n * 0.9):]

    def make_loader(indexes, shuffle):
        records = [tokenized[i] for i in indexes]

        def collate(batch):
            max_len = max(len(ids) for ids, _ in batch)
            pad = tokenizer.pad_token_id
            input_ids, attention_mask, labels = [], [], []
            for ids, labs in batch:
                p = max_len - len(ids)
                input_ids.append(ids + [pad] * p)
                attention_mask.append([1] * len(ids) + [0] * p)
                labels.append(labs + [-100] * p)
            return (torch.tensor(input_ids, device=device),
                    torch.tensor(attention_mask, device=device),
                    torch.tensor(labels, device=device))

        return DataLoader(records, batch_size=8, shuffle=shuffle, collate_fn=collate)

    train_loader = make_loader(train_idx, True)
    val_loader = make_loader(val_idx, False)
    test_loader = make_loader(test_idx, False)

    _disable_incompatible_torchvision()
    model = AutoModelForTokenClassification.from_pretrained(source, num_labels=len(LABELS))
    model.config.id2label = ID2LABEL
    model.config.label2id = LABEL2ID
    model.to(device)

    # 类别权重：缓解样本不平衡（O 与 Disease 占比较大，小众类型如 RiskFactor 需加权）
    cnt = {i: 0 for i in range(len(LABELS))}
    for _, labs in tokenized:
        for l in labs:
            if l != -100:
                cnt[l] += 1
    total = sum(cnt.values())
    weight = torch.tensor(
        [total / max(cnt[i], 1) for i in range(len(LABELS))], dtype=torch.float32, device=device
    )
    weight[LABEL2ID["O"]] = 1.0  # 压低 O 类权重，避免模型过度倾向预测 O

    optimizer = AdamW(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss(ignore_index=-100, weight=weight)

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for input_ids, attention_mask, labels in train_loader:
            optimizer.zero_grad()
            logits = model(input_ids=input_ids, attention_mask=attention_mask).logits
            loss = loss_fn(logits.view(-1, len(LABELS)), labels.view(-1))
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        print(f"epoch {epoch + 1}/{epochs}: loss={total_loss / max(len(train_loader), 1):.4f}", flush=True)

    # 评测（test 集 span F1）
    model.eval()
    gold_spans, pred_spans = [], []
    with torch.no_grad():
        for input_ids, attention_mask, labels in test_loader:
            preds = torch.argmax(model(input_ids=input_ids, attention_mask=attention_mask).logits, dim=-1)
            for i in range(len(labels)):
                # 只取“真实 token + 非忽略位”，保证 gold 与 pred 索引对齐
                valid = [k for k in range(len(attention_mask[i]))
                         if attention_mask[i][k] == 1 and labels[i][k] != -100]
                gold_spans.append(_decode_labels([int(labels[i][k]) for k in valid]))
                pred_spans.append(_decode_labels([int(preds[i][k]) for k in valid]))
    p, r, f1 = _span_f1(gold_spans, pred_spans)
    print(f"test — Precision={p:.4f} Recall={r:.4f} F1={f1:.4f}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(MODEL_DIR))
    tokenizer.save_pretrained(str(MODEL_DIR))
    print(f"模型已保存到 {MODEL_DIR}")

    # 训练完成（模型已保存）后才递增 trained：把参与本轮训练的审查三元组计数 +1
    _bump_trained_ids(samples)
    return 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="训练 BERT NER 模型")
    ap.add_argument("--data", default=str(MERGED_OUT),
                    help=f"训练数据 BIO 文件（默认合并集，可选 CMeEE: {CME_LABELS.name}）")
    ap.add_argument("--epochs", type=int, default=3, help="训练轮数（默认 3）")
    ap.add_argument("--resume", action="store_true",
                    help="从 models/ner 已有权重继续训练（增量微调，小学习率）")
    args = ap.parse_args()

    # 显式指定 --data 时直接用它训练，不再重建/覆盖训练集文件；
    # 否则 build_dataset/merge_dataset 会覆盖 ner_train_labels.json，
    # 冲掉先前 build_review_data.py merge 进去的人工审查样本（source_ids 随之丢失）。
    if args.data != str(MERGED_OUT):
        return train(data_path=Path(args.data), epochs=args.epochs, resume=args.resume)

    build_dataset()
    merge_dataset()
    return train(data_path=Path(args.data), epochs=args.epochs, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())