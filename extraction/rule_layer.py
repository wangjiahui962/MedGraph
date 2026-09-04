# -*- coding: utf-8 -*-
"""【第三阶段 · 规则层（确定性任务）】

在第一阶段本体、第二阶段预处理之后，用"医学词典 + 规则模板"做确定性抽取：

- 主体实体：以文档标题作为“疾病(Disease)”主体（本项目采样文本为疾病类文档）。
- 客体实体：用词典命中 + 少量后缀模板，从正文识别
    症状(Symptom) / 药物(Drug) / 治疗方法(Treatment) / 检查方法(Examination) / 危险因素(RiskFactor)。
- 关系：按客体类型映射到本体中的规范关系：
    症状 -> HAS_SYMPTOM；药物/治疗 -> TREATED_BY；检查 -> DIAGNOSED_BY；危险因素 -> HAS_RISK_FACTOR。

规则层只负责“确定性”部分（词典/模板能确定命中的事实），不做复杂语义与消歧；
这类任务交给深度学习层 / LLM 层完成。

输出三元组结构与分层编排层（extract.py）约定一致，字段说明见 extract.py 文档字符串。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology import schema  # noqa: E402

# 规则层开关：默认关闭。
# 关闭原因：规则层把"标题当疾病主语"（概念页/事件页标题如"带原者""2019冠状病毒病山东省疫情"
# 也被当作疾病）且后缀模板有吞字噪声，质量差、与深度学习层结果大量重复，信息抽取不再使用。
# 注意：本模块的医学词典（SYMPTOM_TERMS / DRUG_TERMS 等）仍被深度学习层做词面校验、
# 被 dl_train 做弱监督数据复用，这些不受本开关影响；开关只控制 extract() 是否产出三元组。
AVAILABLE = False

# ---------------------------------------------------------------------------
# 医学词典：按目标客体实体类型组织的术语表（可在扩大语料后继续补充）
# ---------------------------------------------------------------------------
SYMPTOM_TERMS: set[str] = {
    "咳嗽", "喘息", "胸闷", "胸痛", "呼吸困难", "呼吸短促", "气急", "气促",
    "咳痰", "多痰", "发热", "头痛", "乏力", "嗜睡", "紫绀", "心悸",
    "恶心", "呕吐", "腹泻", "便秘", "失眠", "焦虑", "抑郁", "水肿", "皮疹",
    "鼻鼾", "打鼾",
}

DRUG_TERMS: set[str] = {
    "沙丁胺醇", "糖皮质激素", "皮质类固醇", "抗生素", "红霉素", "抗抑郁药",
    "镇静催眠药", "胰岛素", "布地奈德", "阿司匹林", "β2激动药", "β受体阻滞剂",
    "白三烯拮抗药", "硫酸镁",
}

TREATMENT_TERMS: set[str] = {
    "戒烟", "康复治疗", "放射治疗", "化疗", "手术", "心理治疗", "肺移植",
    "吸氧", "接种疫苗", "药物治疗", "吸入治疗", "住院治疗", "静脉注射",
}

EXAMINATION_TERMS: set[str] = {
    "呼吸量测定法", "肺功能检查", "肺功能测试", "CT检查", "核磁共振",
    "血常规", "基因检测", "医学筛查", "影像学检查",
}

RISK_FACTOR_TERMS: set[str] = {
    "吸烟", "空气污染", "过敏原", "基因", "遗传", "遗传因素", "感染",
    "病毒", "细菌", "高血压", "糖尿病", "肥胖", "饮酒", "压力",
    "环境因素", "血栓", "放射",
}

# ---------------------------------------------------------------------------
# 后缀模板：词典之外的确定性补充（命中置信度略低于词典命中）
#   - "XX检查 / XX测定法 / XX检测 / XX筛查" -> 检查方法
#   - "XX治疗 / XX疗法"                       -> 治疗方法
# ---------------------------------------------------------------------------
_EXAMINATION_PATTERN = re.compile(r"([\u4e00-\u9fff]{2,10}(?:检查|测定法|检测|筛查))")
_TREATMENT_PATTERN = re.compile(r"([\u4e00-\u9fff]{2,10}(?:治疗|疗法))")

# 词典命中 / 模板命中的置信度（规则层为确定性，置信度相对固定）
LEXICON_CONFIDENCE = 0.90
PATTERN_CONFIDENCE = 0.70

# 模板命中中不应出现的"吞字标记"（虚词/助词/连接词）：
# 后缀模板"XX治疗/XX检查/XX筛查"容易把整句成分贪心吞进来，例如
#   "并发明出有效的治疗"  "出于流行病学筛查"
# 这些都不是真实医学术语。术语内含任意一个标记字即视为模板吞字噪声，直接丢弃
# （真实术语如"药物治疗/住院治疗/血常规检查/流行病学筛查"不含这些字）。
# 注意：集合只收"虚词/连接词"，不收"中/经/药"等可出现在合法术语中的字，
# 避免误伤"中医治疗""经络治疗"这类结果。
_TEMPLATE_NOISE = frozenset(
    "的地得之了着过出并于等或和及与则也都而其为被把将从对在是有"
    "这那尚再既且但虽还另须"
)


def _clean_template_term(term: str) -> str | None:
    """过滤模板贪心吞字的噪声命中：仍含吞字标记则返回 None，否则返回术语本身。

    例如"并发明出有效的治疗""出于流行病学筛查"含"出/的/于"等标记 -> 丢弃；
    "药物治疗""流行病学筛查"不含标记 -> 保留。
    """
    t = (term or "").strip()
    if not t or any(ch in _TEMPLATE_NOISE for ch in t):
        return None
    return t


def _sentences_of(record: dict[str, Any]) -> list[str]:
    """取预处理后的句子列表；缺失时退化为简单分句兜底。"""
    sentences = record.get("sentences")
    if isinstance(sentences, list) and sentences:
        return [s for s in sentences if s]
    content = record.get("content") or ""
    parts = re.split(r"(?<=[。！？!?；;])", content)
    return [p.strip() for p in parts if p.strip()]


def _lexicon_matches(terms: set[str], sentences: list[str]) -> list[tuple[str, str]]:
    """返回 (术语, 证据句) 列表；每个术语只保留第一条命中的句子作为证据。"""
    found: list[tuple[str, str]] = []
    for term in terms:
        for sent in sentences:
            if term in sent:
                found.append((term, sent))
                break
    return found


def _pattern_matches(pattern: re.Pattern[str], sentences: list[str]) -> list[tuple[str, str]]:
    """用后缀模板抽取术语，返回 (术语, 证据句) 列表。"""
    found: list[tuple[str, str]] = []
    for sent in sentences:
        for m in pattern.findall(sent):
            found.append((m, sent))
    return found


def _make_triple(
    subject: str,
    subject_type: str,
    relation: str,
    object_: str,
    object_type: str,
    document_id: str | None,
    source_text: str,
    confidence: float,
    layer: str = "rule",
) -> dict[str, Any]:
    """构造一条三元组，同时携带规范英文 ID 与中文标签（供展示/入库使用）。"""
    return {
        "subject": subject,
        "subject_type": subject_type,
        "subject_type_label": schema.entity_type_label(subject_type),
        "relation": relation,
        "relation_label": schema.relation_label(relation),
        "object": object_,
        "object_type": object_type,
        "object_type_label": schema.entity_type_label(object_type),
        "source_document_id": document_id,
        "source_text": source_text,
        "confidence": confidence,
        "layer": layer,
    }


def extract(record: dict[str, Any]) -> list[dict[str, Any]]:
    """对单条预处理文档执行规则层抽取，返回三元组列表。

    模块关闭（AVAILABLE=False）时直接返回空列表，实现全局禁用规则层信息抽取。
    """
    if not AVAILABLE:
        return []

    title = (record.get("title") or "").strip()
    document_id = record.get("document_id")
    if not title:
        return []

    sentences = _sentences_of(record)
    subject_type = "Disease"  # 采样文档为疾病类，标题即疾病主体

    triples: list[dict[str, Any]] = []

    # 症状 -> HAS_SYMPTOM
    for term, sent in _lexicon_matches(SYMPTOM_TERMS, sentences):
        triples.append(_make_triple(title, subject_type, "HAS_SYMPTOM", term, "Symptom",
                                    document_id, sent, LEXICON_CONFIDENCE))

    # 药物 -> TREATED_BY（宾语类型 Drug）
    for term, sent in _lexicon_matches(DRUG_TERMS, sentences):
        triples.append(_make_triple(title, subject_type, "TREATED_BY", term, "Drug",
                                    document_id, sent, LEXICON_CONFIDENCE))

    # 治疗方法 -> TREATED_BY（宾语类型 Treatment）
    for term, sent in _lexicon_matches(TREATMENT_TERMS, sentences):
        triples.append(_make_triple(title, subject_type, "TREATED_BY", term, "Treatment",
                                    document_id, sent, LEXICON_CONFIDENCE))
    for term, sent in _pattern_matches(_TREATMENT_PATTERN, sentences):
        term = _clean_template_term(term)
        if not term:
            continue
        triples.append(_make_triple(title, subject_type, "TREATED_BY", term, "Treatment",
                                    document_id, sent, PATTERN_CONFIDENCE))

    # 检查方法 -> DIAGNOSED_BY
    for term, sent in _lexicon_matches(EXAMINATION_TERMS, sentences):
        triples.append(_make_triple(title, subject_type, "DIAGNOSED_BY", term, "Examination",
                                    document_id, sent, LEXICON_CONFIDENCE))
    for term, sent in _pattern_matches(_EXAMINATION_PATTERN, sentences):
        term = _clean_template_term(term)
        if not term:
            continue
        triples.append(_make_triple(title, subject_type, "DIAGNOSED_BY", term, "Examination",
                                    document_id, sent, PATTERN_CONFIDENCE))

    # 危险因素/病因 -> HAS_RISK_FACTOR
    for term, sent in _lexicon_matches(RISK_FACTOR_TERMS, sentences):
        triples.append(_make_triple(title, subject_type, "HAS_RISK_FACTOR", term, "RiskFactor",
                                    document_id, sent, LEXICON_CONFIDENCE))

    return triples