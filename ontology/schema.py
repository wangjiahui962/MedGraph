# -*- coding: utf-8 -*-
"""【第一阶段 · 本体设计（Ontology Design）】

本文件定义信息抽取链路所需的“本体（Ontology）”，是整个流水线
（数据预处理 → 分层信息抽取 → 知识融合 → 知识存储）的**统一参照 Schema**。

本体包含三类约定：
    1. 实体类型（Entity Types）：图谱中有哪些“节点类型”，以及每种节点允许携带的属性。
    2. 关系类型（Relation Types）：图谱中有哪些“边类型”，以及每条边允许连接的头/尾节点
       类型（domain / range 约束）。
    3. 属性模式（Attribute Schemas）：节点 / 关系上可出现的属性名、取值类型与是否必需。

------------------------------------------------------------
关于“R 大类文本”的领域落实：
------------------------------------------------------------
题目中的“R 大类”指《中国图书馆分类法》中的“医药卫生（R 类）”，因此本项目
按**医疗领域**落实本体（项目名 MedGraph 也与之一致）。

知识图谱项目任务.txt 中示例的通用本体
    person / Organization / Location / Concept / event / publication
以及通用关系
    worksAt / locatedIn / authors / mentions / relatedTo
属于“通用知识图谱本体”的举例，与本项目实际采集的医药卫生文本不匹配，
故不直接采用；本文件将其中可对应的部分（如 relatedTo）保留为兜底的
对称关系 RELATED_TO，其余映射为医疗实体与关系。

本文件同时提供两类接口：
    - resolve_*：把“中文标签”或“英文 ID”统一解析为规范英文 ID；
    - validate_* / is_valid_relation：校验实体类型、关系是否合法，
      以及关系 domain/range 是否匹配。
运行 `python ontology/schema.py` 会打印本体摘要，并将冻结后的
本体导出为 `data/ontology.json`，供后续阶段复用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "ontology.json"

# ---------------------------------------------------------------------------
# 1. 实体类型（Entity Types）
#    键为规范英文 ID；值为：
#        label      —— 中文标签（对应 data/processed/triples.json 中的类型名）
#        attributes —— 属性模式：属性名 -> {type: 取值类型, desc: 说明, required: 是否必需}
# ---------------------------------------------------------------------------
ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "Disease": {
        "label": "疾病",
        "attributes": {
            "name":        {"type": "str",       "desc": "规范名称",     "required": True},
            "aliases":     {"type": "list[str]", "desc": "别名列表",     "required": False},
            "definition":  {"type": "str",       "desc": "定义/概述",    "required": False},
            "icd_code":    {"type": "str",       "desc": "ICD 编码",     "required": False},
            "category_id": {"type": "str",       "desc": "所属类别",     "required": False},
        },
    },
    "Symptom": {
        "label": "症状",
        "attributes": {
            "name":      {"type": "str", "desc": "症状名",   "required": True},
            "body_site": {"type": "str", "desc": "发生部位", "required": False},
            "severity":  {"type": "str", "desc": "严重程度", "required": False},
        },
    },
    "Drug": {
        "label": "药物",
        "attributes": {
            "name":         {"type": "str", "desc": "通用名",    "required": True},
            "generic_name": {"type": "str", "desc": "通用名(英)", "required": False},
            "dosage_form":  {"type": "str", "desc": "剂型",      "required": False},
        },
    },
    "Treatment": {
        "label": "治疗方法",
        "attributes": {
            "name":           {"type": "str", "desc": "治疗/措施名", "required": True},
            "treatment_type": {"type": "str", "desc": "治疗分类",    "required": False},
        },
    },
    "Examination": {
        "label": "检查方法",
        "attributes": {
            "name":   {"type": "str", "desc": "检查名称", "required": True},
            "method": {"type": "str", "desc": "检查手段", "required": False},
        },
    },
    "Department": {
        "label": "科室",
        "attributes": {
            "name": {"type": "str", "desc": "科室名", "required": True},
        },
    },
    "Population": {
        "label": "人群",
        "attributes": {
            "name":      {"type": "str", "desc": "人群名称", "required": True},
            "age_range": {"type": "str", "desc": "年龄段",   "required": False},
        },
    },
    "RiskFactor": {
        "label": "危险因素",
        "attributes": {
            "name":     {"type": "str", "desc": "危险因素名", "required": True},
            "category": {"type": "str", "desc": "因素分类",   "required": False},
        },
    },
    "Complication": {
        "label": "并发症",
        "attributes": {
            "name": {"type": "str", "desc": "并发症名", "required": True},
        },
    },
}

# ---------------------------------------------------------------------------
# 2. 关系类型（Relation Types）
#    键为规范英文 ID；值为：
#        label     —— 中文标签（对应 data/processed/triples.json 中使用的标签）
#        domain    —— 允许的主语实体类型列表（英文 ID）
#        range     —— 允许的宾语实体类型列表（英文 ID）
#        symmetric —— 是否为对称关系（如“相关/共同发生”）
# ---------------------------------------------------------------------------
RELATION_TYPES: dict[str, dict[str, Any]] = {
    "HAS_SYMPTOM": {
        "label": "常见症状",
        "domain": ["Disease"],
        "range": ["Symptom"],
        "symmetric": False,
    },
    "TREATED_BY": {
        "label": "治疗",
        "domain": ["Disease"],
        "range": ["Drug", "Treatment"],
        "symmetric": False,
    },
    "DIAGNOSED_BY": {
        "label": "检查方法",
        "domain": ["Disease"],
        "range": ["Examination"],
        "symmetric": False,
    },
    "HAS_RISK_FACTOR": {
        "label": "病因",
        "domain": ["Disease"],
        "range": ["RiskFactor"],
        "symmetric": False,
    },
    "HAS_SIDE_EFFECT": {
        "label": "不良反应",
        "domain": ["Drug"],
        "range": ["Symptom"],
        "symmetric": False,
    },
    "BELONGS_TO": {
        "label": "所属科室",
        "domain": ["Disease"],
        "range": ["Department"],
        "symmetric": False,
    },
    "HIGH_RISK_FOR": {
        "label": "高危人群",
        "domain": ["Population"],
        "range": ["Disease"],
        "symmetric": False,
    },
    "MAY_CAUSE": {
        "label": "可致并发症",
        "domain": ["Disease"],
        "range": ["Complication"],
        "symmetric": False,
    },
    # 兜底/通用关系：对应任务.txt 中的 relatedTo（相关），对称、不限具体类型
    "RELATED_TO": {
        "label": "相关",
        "domain": list(ENTITY_TYPES.keys()),
        "range": list(ENTITY_TYPES.keys()),
        "symmetric": True,
    },
}

# 所有实体/关系的规范 ID 集合
ALL_ENTITY_TYPE_IDS = frozenset(ENTITY_TYPES.keys())
ALL_RELATION_IDS = frozenset(RELATION_TYPES.keys())

# 中文标签 -> 英文 ID 的反向索引，供 resolve_* 使用
_ENTITY_LABEL_TO_ID: dict[str, str] = {info["label"]: key for key, info in ENTITY_TYPES.items()}
_RELATION_LABEL_TO_ID: dict[str, str] = {info["label"]: key for key, info in RELATION_TYPES.items()}


def resolve_entity_type(value: str | None) -> str | None:
    """把实体类型的中文标签或英文 ID（不区分大小写）解析为规范英文 ID。

    支持形如 "疾病"、"Disease"、"disease" 等输入；无法解析时返回 None。
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v in ENTITY_TYPES:
        return v
    if v in _ENTITY_LABEL_TO_ID:
        return _ENTITY_LABEL_TO_ID[v]
    upper = v.upper()
    for eid in ENTITY_TYPES:
        if eid.upper() == upper:
            return eid
    return None


def resolve_relation(value: str | None) -> str | None:
    """把关系的中文标签或英文 ID（不区分大小写）解析为规范英文 ID。"""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v in RELATION_TYPES:
        return v
    if v in _RELATION_LABEL_TO_ID:
        return _RELATION_LABEL_TO_ID[v]
    upper = v.upper()
    for rid in RELATION_TYPES:
        if rid.upper() == upper:
            return rid
    return None


def entity_type_label(type_id: str) -> str:
    """返回实体类型的规范中文标签，未知时原样返回。"""
    resolved = resolve_entity_type(type_id)
    return ENTITY_TYPES[resolved]["label"] if resolved else type_id


def relation_label(rel_id: str) -> str:
    """返回关系的规范中文标签，未知时原样返回。"""
    resolved = resolve_relation(rel_id)
    return RELATION_TYPES[resolved]["label"] if resolved else rel_id


def is_valid_entity_type(type_id: str) -> bool:
    """判断给定值是否为合法的实体类型。"""
    return resolve_entity_type(type_id) is not None


def is_valid_relation(
    rel_id: str,
    subject_type: str | None = None,
    object_type: str | None = None,
) -> bool:
    """校验关系是否合法，并检查其 domain/range 约束。

    - rel_id 必须能解析为已定义的关系；
    - 若提供 head_type / tail_type，则一并校验其是否符合该关系的头/尾类型约束
      （subject 属于 domain，object 属于 range）。
    """
    rel = resolve_relation(rel_id)
    if rel is None:
        return False
    info = RELATION_TYPES[rel]
    sub = resolve_entity_type(subject_type) if subject_type else None
    obj = resolve_entity_type(object_type) if object_type else None
    if sub is not None and sub not in info["domain"]:
        return False
    if obj is not None and obj not in info["range"]:
        return False
    return True


def export() -> dict[str, Any]:
    """把本体序列化为可落盘的字典（entity_types / relation_types 两大部分）。"""
    return {
        "entity_types": ENTITY_TYPES,
        "relation_types": RELATION_TYPES,
    }


def main() -> int:
    """打印本体摘要，并将本体冻结导出为 data/ontology.json。"""
    print(f"实体类型（{len(ENTITY_TYPES)} 类）：")
    for eid, info in ENTITY_TYPES.items():
        attrs = "、".join(info["attributes"].keys())
        print(f"  - {eid:<14} 标签={info['label']:<6} 属性=[{attrs}]")

    print(f"\n关系类型（{len(RELATION_TYPES)} 类）：")
    for rid, info in RELATION_TYPES.items():
        src = "/".join(info["domain"])
        dst = "/".join(info["range"])
        sym = "（对称）" if info["symmetric"] else ""
        print(f"  - {rid:<16} 标签={info['label']:<6} {src} -> {dst}{sym}")

    # 自检：确认所有 domain/range 引用的实体类型都已定义
    for rid, info in RELATION_TYPES.items():
        for t in info["domain"] + info["range"]:
            assert t in ENTITY_TYPES, f"关系 {rid} 引用了未定义的实体类型 {t}"

    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.write_text(
        json.dumps(export(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n本体已冻结导出：{DEFAULT_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())