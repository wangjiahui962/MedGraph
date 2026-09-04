# -*- coding: utf-8 -*-
"""【第二阶段 · 数据预处理（Data Preprocessing）】

在“第一阶段本体设计”之后、正式抽取之前，对采集到的原始文本做流水线式清洗，
输出结构化的“标准文档”，供第三阶段分层信息抽取使用。

处理流水线（与知识图谱项目任务.txt 保持一致）：
    1. 文本清洗（clean_text）       —— 去 HTML 残留、控制符，Unicode 归一化，压缩空白
    2. 分句分词（分句/分词）         —— 按中英文句末标点切分句子，再对每句做中文分词
    3. 结构化字段提取（extract_fields） —— 抽出标题/类别/清洗后正文及长度、句数、
                                       词数、中文占比等结构化字段
    4. 置信度标注（annotate_confidence） —— 依据长度、完整性、可读性给出清洗后置信度
                                        （clean_confidence）与高/中/低等级

输入数据来源（按优先级）：
    - data/documents.db 的 documents 表（若存在且非空）
    - data/raw/medical_sample.json（采集模块直接产出，作为兜底）
输出：
    - data/processed/preprocessed_documents.json —— 清洗后的标准文档列表

注意：本阶段只做“文本层面的清洗与结构化”，**不做实体识别/关系抽取**；
实体与关系由第三阶段基于 ontology/schema.py 完成，届时会引用本阶段产出的字段。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

# 分词可选依赖：优先使用 jieba 做中文分词，缺失时退化为朴素词块切分
try:
    import jieba

    jieba.setLogLevel(20)  # 关闭 jieba 的加载日志
except ImportError:  # pragma: no cover - 依赖缺失时为可选降级路径
    jieba = None

# 繁简转换可选依赖：opencc-python-reimplemented（纯 Python，无需编译）。
# 源语料含繁体（如"霍亂/傳染性疾病/隔離/數據蒐集"），统一转简体后再进入抽取，
# 保证规则层简体词典、DL 简体语料训练的 NER 模型、LLM 提示词口径一致，
# 避免同义词因简繁书写不同而分裂（如"隔离"vs"隔離"）。
try:
    from opencc import OpenCC as _OpenCC

    _T2S = _OpenCC("t2s")
except Exception:  # pragma: no cover - 依赖缺失时为可选降级路径（保留繁体）
    _T2S = None


def _to_simplified(text: str) -> str:
    """繁体 -> 简体（opencc t2s）；未安装 opencc 时原样返回。"""
    if _T2S is None:
        return text
    try:
        return _T2S.convert(text)
    except Exception:  # pragma: no cover - 单条转换失败不应中断整体流程
        return text

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS_DB = ROOT / "data" / "documents.db"
RAW_JSON = ROOT / "data" / "raw" / "medical_sample.json"
OUTPUT = ROOT / "data" / "processed" / "preprocessed_documents.json"

# documents 表字段顺序（与 db/init_db.py 保持一致）
DOCUMENT_COLUMNS = [
    "document_id",
    "category_ids",
    "title",
    "content",
    "source_url",
    "license",
    "collected_at",
    "content_hash",
    "quality_score",
]

# ---------------------------------------------------------------------------
# 1. 文本清洗
# ---------------------------------------------------------------------------
_HTML_TAG = re.compile(r"<[^>]*>")                       # HTML 标签
_HTML_ENTITY = re.compile(r"&[a-zA-Z#0-9]+;")            # HTML 实体，如 &nbsp;
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # 控制字符
_WHITESPACE = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    """清洗单段文本：NFKC 归一化 -> 繁转简 -> 去 HTML -> 去控制符 -> 压缩空白。"""
    # 1) Unicode NFKC 归一化：全角转半角、兼容字符规整（如 Ａ->A、①->1）
    text = unicodedata.normalize("NFKC", text or "")
    # 2) 繁体 -> 简体（可选依赖；保证规则层词典 / DL 模型 / LLM 提示词口径一致）
    text = _to_simplified(text)
    # 3) 去除 HTML 实体与标签（正文理论上已是纯文本，此处作为兜底防御）
    text = _HTML_ENTITY.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    # 4) 去除控制字符
    text = _CONTROL_CHARS.sub("", text)
    # 5) 压缩连续空白为单个空格
    text = _WHITESPACE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# 2. 分句 / 分词
# ---------------------------------------------------------------------------
# 中文句末标点（。！？）与英文句末标点（! ?）及分号；句末标点后切分、保留在上一句。
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
# 中文字符范围，用于统计“中文占比”
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
# 朴素词块：连续中文 / 连续英数（可含 .- 连字符）/ 非空白非中文字符（标点）
_FALLBACK_TOKEN = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+(?:[-.][a-zA-Z0-9]+)*|[^\s\u4e00-\u9fffa-zA-Z0-9]")


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点分句，返回去除空白后的句子列表。"""
    parts = [p.strip() for p in _SENT_SPLIT.split(text)]
    return [p for p in parts if p]


def tokenize_sentence(sentence: str) -> list[str]:
    """对单个句子分词。

    优先使用 jieba；未安装时退化为“连续中文 / 英数词块 / 标点”的朴素切分，
    后者不具备词典消歧能力，仅保证流程可运行。
    """
    if jieba is not None:
        return [tok for tok in jieba.lcut(sentence) if tok.strip()]
    return _FALLBACK_TOKEN.findall(sentence)


# ---------------------------------------------------------------------------
# 3. 结构化字段提取
# ---------------------------------------------------------------------------

def _normalize_category_ids(value: Any) -> list[str]:
    """把 category_ids 统一为字符串列表（db 中为 JSON 字符串，raw 中为 list）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            return [value]
    return []


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """把来自 db 或 raw 的记录字段补齐/规整为统一结构。"""
    return {
        "document_id": record.get("document_id"),
        "category_ids": _normalize_category_ids(record.get("category_ids")),
        "title": record.get("title"),
        "content": record.get("content"),
        "source_url": record.get("source_url"),
        "license": record.get("license"),
        "collected_at": record.get("collected_at"),
        "content_hash": record.get("content_hash"),
        "quality_score": record.get("quality_score"),
    }


def extract_fields(
    record: dict[str, Any],
    cleaned_content: str,
    sentences: list[str],
    tokens_per_sentence: list[list[str]],
) -> dict[str, Any]:
    """把一条文档整理为结构化字段（标题/类别/正文及若干衍生统计）。"""
    total_chars = len(cleaned_content)
    cjk_chars = len(_CJK_CHAR.findall(cleaned_content))
    cjk_ratio = (cjk_chars / total_chars) if total_chars else 0.0
    tokens = [tok for sent_tokens in tokens_per_sentence for tok in sent_tokens]

    return {
        # —— 基础字段（对齐 ontology 中的 Disease/元数据） ——
        "document_id": record.get("document_id"),
        "category_ids": record.get("category_ids"),
        "title": clean_text(record.get("title")),
        "content": cleaned_content,
        "source_url": record.get("source_url"),
        "license": record.get("license"),
        "collected_at": record.get("collected_at"),
        "content_hash": record.get("content_hash"),
        "source_quality_score": record.get("quality_score"),
        # —— 清洗后衍生统计字段 ——
        "content_length": total_chars,
        "sentence_count": len(sentences),
        "token_count": len(tokens),
        "cjk_ratio": round(cjk_ratio, 4),
        # —— 供第三阶段直接使用的分句/分词结果 ——
        "sentences": sentences,
        "tokens": tokens,
    }


# ---------------------------------------------------------------------------
# 4. 置信度标注
# ---------------------------------------------------------------------------

def annotate_confidence(fields: dict[str, Any]) -> dict[str, Any]:
    """依据长度、中文占比、句数、是否含标题等，计算清洗后置信度（0~1）。

    阈值含义（与设计方案中的质量分级思路一致，但本阶段衡量的是“清洗质量”）：
        high   （>= 0.75）：文本完整、正文充足，可直接进入抽取；
        medium （>= 0.50）：基本可用，建议抽取后进入人工复核队列；
        low    （<  0.50）：质量不足，默认不作为可靠抽取输入。
    """
    score = 0.0

    length = fields["content_length"]
    if length >= 120:
        score += 0.35
    elif length >= 60:
        score += 0.20
    else:
        score += 0.05

    # 中文占比：占比越高越符合“R 大类中文文本”预期
    score += 0.30 * min(fields["cjk_ratio"] / 0.9, 1.0)

    # 分句数量：句子越完整越利于分句级抽取
    sentence_count = fields["sentence_count"]
    if sentence_count >= 2:
        score += 0.20
    elif sentence_count == 1:
        score += 0.10

    # 具备标题且正文非空是基础完整性
    if fields["title"]:
        score += 0.15

    clean_confidence = round(min(max(score, 0.0), 1.0), 4)
    if clean_confidence >= 0.75:
        level = "high"
    elif clean_confidence >= 0.50:
        level = "medium"
    else:
        level = "low"

    fields["clean_confidence"] = clean_confidence
    fields["confidence_level"] = level
    return fields


def preprocess_record(record: dict[str, Any]) -> dict[str, Any]:
    """执行单条文档的完整预处理流水线，返回结构化+置信度标注后的字典。"""
    rec = _normalize_record(record)
    cleaned = clean_text(rec.get("content"))
    sentences = split_sentences(cleaned)
    tokens_per_sentence = [tokenize_sentence(s) for s in sentences]
    fields = extract_fields(rec, cleaned, sentences, tokens_per_sentence)
    return annotate_confidence(fields)


# ---------------------------------------------------------------------------
# 输入加载
# ---------------------------------------------------------------------------

def load_documents() -> list[dict[str, Any]]:
    """优先读取 documents.db，其次回退到 medical_sample.json。"""
    if DOCUMENTS_DB.is_file():
        try:
            conn = sqlite3.connect(DOCUMENTS_DB)
            cols_sql = ", ".join(DOCUMENT_COLUMNS)
            rows = conn.execute(f"SELECT {cols_sql} FROM documents").fetchall()
            conn.close()
            if rows:
                return [dict(zip(DOCUMENT_COLUMNS, row)) for row in rows]
        except sqlite3.Error as exc:  # db 损坏/结构不符时回退到 raw JSON
            print(f"WARN: 读取 {DOCUMENTS_DB.name} 失败（{exc}），回退到 raw JSON", file=sys.stderr)

    if RAW_JSON.is_file():
        data = json.loads(RAW_JSON.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data

    return []


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="数据预处理")
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 篇文档（按 documents.db 顺序；0=全部）")
    args = ap.parse_args()

    records = load_documents()
    if not records:
        print("ERROR: 没有可预处理的文档（documents.db 与 medical_sample.json 均为空）", file=sys.stderr)
        return 1

    if args.limit > 0:
        records = records[:args.limit]

    processed = [preprocess_record(rec) for rec in records]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(processed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # —— 简单统计，便于与原始文档、后续抽取结果核对 ——
    total = len(processed)
    avg_length = sum(p["content_length"] for p in processed) / total
    high = sum(1 for p in processed if p["confidence_level"] == "high")
    medium = sum(1 for p in processed if p["confidence_level"] == "medium")
    low = total - high - medium

    print(f"预处理完成，共处理 {total} 条文档")
    print(f"  - 平均正文长度：{avg_length:.1f} 字符")
    print(f"  - 置信度分布：high={high}、medium={medium}、low={low}")
    print(f"  - 输出文件：{OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())