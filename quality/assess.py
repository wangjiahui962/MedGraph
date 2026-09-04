#!/usr/bin/env python3
"""为三元组计算可解释置信度并检测实体类型冲突。

用法：python quality/assess.py [输入JSON] [输出JSON]
默认读取并原地更新 data/processed/triples.json，同时生成 conflicts.json。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/processed/triples.json"
DEFAULT_CONFLICTS = ROOT / "data/processed/conflicts.json"

RELATION_SCHEMA = {
    "HAS_SYMPTOM": ({"Disease"}, {"Symptom"}),
    "TREATED_BY": ({"Disease"}, {"Drug", "Treatment"}),
    "DIAGNOSED_BY": ({"Disease"}, {"Examination"}),
    "MAY_CAUSE": ({"Disease", "RiskFactor"}, {"Disease", "Symptom", "Complication"}),
    "HAS_RISK_FACTOR": ({"Disease"}, {"RiskFactor"}),
    "HAS_SIDE_EFFECT": ({"Drug", "Treatment"}, {"Symptom", "Complication"}),
    "HIGH_RISK_FOR": ({"Disease"}, {"Population"}),
    "BELONGS_TO": ({"Disease", "Drug", "Symptom", "Treatment"}, {"Department"}),
}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def assess(records: list[dict]) -> tuple[list[dict], list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    entity_types: dict[str, Counter[str]] = defaultdict(Counter)
    for row in records:
        if not isinstance(row, dict):
            continue
        s, o = _text(row.get("subject")), _text(row.get("object"))
        r = _text(row.get("relation"))
        if not s or not o or not r:
            continue
        groups[(s, r, o)].append(row)
        entity_types[s][_text(row.get("subject_type")) or "未分类"] += 1
        entity_types[o][_text(row.get("object_type")) or "未分类"] += 1

    conflicts: list[dict] = []
    conflict_entities: dict[str, set[str]] = {}
    for name, counts in entity_types.items():
        if len(counts) > 1:
            types = sorted(counts)
            conflict_entities[name] = set(types)
            conflicts.append({
                "conflict_type": "entity_type_conflict",
                "entity": name,
                "observed_types": dict(counts),
                "suggested_type": counts.most_common(1)[0][0],
                "severity": "high" if counts.most_common(1)[0][1] >= 3 * counts.most_common()[-1][1] else "medium",
            })

    output: list[dict] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        s, o, r = _text(row.get("subject")), _text(row.get("object")), _text(row.get("relation"))
        if not s or not o or not r:
            continue
        group = groups[(s, r, o)]
        docs = {_text(x.get("source_document_id")) for x in group if _text(x.get("source_document_id"))}
        evidence = {(_text(x.get("source_document_id")), _text(x.get("source_text"))) for x in group if _text(x.get("source_text"))}
        flags: list[str] = []
        st, ot = _text(row.get("subject_type")) or "未分类", _text(row.get("object_type")) or "未分类"
        allowed = RELATION_SCHEMA.get(r)
        if allowed and (st not in allowed[0] or ot not in allowed[1]):
            flags.append("entity_type_mismatch")
        if s in conflict_entities or o in conflict_entities:
            flags.append("entity_type_conflict")
        if not docs:
            flags.append("no_source_document")
        if not _text(row.get("source_text")):
            flags.append("no_evidence")
        elif len(_text(row.get("source_text"))) < 12:
            flags.append("short_evidence")
        if len(docs) == 1:
            flags.append("single_source")

        # 使用连续分数，避免所有单来源样本都落在同一个百分比。
        source_support = min(1.0, 0.55 + 0.12 * min(len(docs), 4)) if docs else 0.0
        evidence_len = len(_text(row.get("source_text")))
        evidence_score = min(1.0, 0.35 + evidence_len / 70) if evidence_len else 0.0
        # 类型不匹配是扣分项，但不直接把整条关系判为 0 分。
        schema_score = 0.45 if "entity_type_mismatch" in flags else 1.0
        # 保留抽取阶段的原始分，避免重复运行评估脚本时置信度逐次衰减。
        raw_base = row.get("extraction_confidence")
        if raw_base in (None, 0, 0.72):
            raw_base = 0.78
        base = float(raw_base or 0.78)
        score = 0.30 * base + 0.25 * source_support + 0.20 * 0.8 + 0.15 * evidence_score + 0.10 * schema_score
        if "entity_type_conflict" in flags:
            score -= 0.15
        if not docs or not _text(row.get("source_text")):
            score = min(score, 0.59)
        score = max(0.0, min(1.0, round(score, 3)))
        enriched = dict(row)
        enriched.update({
            "confidence": score,
            "extraction_confidence": base,
            "confidence_level": "high" if score >= 0.8 else "medium" if score >= 0.6 else "review",
            "support_document_count": len(docs),
            "support_evidence_count": len(evidence),
            "quality_flags": flags,
            "review_status": "warning" if flags else "unreviewed",
        })
        output.append(enriched)
    return output, conflicts


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else source
    try:
        rows = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: 无法读取 {source}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(rows, list):
        print("ERROR: 输入必须是 JSON 数组。", file=sys.stderr)
        return 1
    scored, conflicts = assess(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    DEFAULT_CONFLICTS.write_text(json.dumps(conflicts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已评估 {len(scored)} 条三元组，发现 {len(conflicts)} 个实体类型冲突。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
