#!/usr/bin/env python3
"""Rule-based medical entity/relation extraction for the sample corpus."""
from __future__ import annotations
import argparse, json, re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/raw/medical_sample.json"
DEFAULT_OUTPUT = ROOT / "data/processed/triples.json"

SYMPTOMS = ["咳嗽","咳痰","多痰","喘息","气喘","呼吸困难","呼吸短促","胸闷","胸痛","发热","头痛","头晕","恶心","呕吐","乏力","疲倦","鼻鼾","嗜睡","腹痛","腹泻","便秘","皮疹","瘙痒","水肿","心悸","出汗","昏厥","抽搐"]
DRUGS = ["阿司匹林","β受体阻滞剂","布地奈德","沙丁胺醇","糖皮质激素","抗生素","胰岛素","二甲双胍","降压药","抗凝药","抗抑郁药","止痛药","青霉素","阿莫西林","利尿剂"]
TREATMENTS = ["吸入治疗","药物治疗","手术治疗","放射治疗","化学治疗","康复治疗","心理治疗","生活方式干预","饮食控制","氧疗","机械通气","人工呼吸","戒烟","支持治疗"]
EXAMS = ["肺功能检查","呼吸量测定法","胸部X线","胸部CT","磁共振成像","计算机断层扫描","超声检查","血液检查","血糖检测","心电图","活组织检查","医学筛查","基因检测","体格检查"]
CAUSE_TERMS = ["基因","遗传因素","环境因素","吸烟","空气污染","过敏原","感染","细菌","病毒","肥胖","高血压","糖尿病","血栓","胆固醇"]
RELATION_PATTERNS = {
    "常见症状": [r"(?:症状|征状|表现)(?:包括|有|为|是|主要是|常见的)?", r"出现", r"伴随"],
    "治疗": [r"(?:治疗|处理)(?:包括|采用|使用|给予|可用|方法为)?", r"通过.+治疗"],
    "病因": [r"(?:病因|原因|因素)(?:包括|是|为)?", r"(?:由|因|因为).{0,8}(?:导致|造成|引起)"],
    "不良反应": [r"(?:不良反应|副作用)(?:包括|有|为)?", r"可引起"],
    "检查方法": [r"(?:检查|诊断)(?:包括|基于|采用|可通过|通常基于)?", r"检测", r"测定"],
}

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def sentences(text: str) -> Iterable[str]:
    for part in re.split(r"[。！？；;\n]+", text):
        part = clean(part)
        if part: yield part

def terms_in(text: str, terms: list[str]) -> list[str]:
    return sorted({term for term in terms if term in text}, key=lambda x: (-len(x), x))

def make_triple(subject, relation, obj, object_type, doc_id, source_text):
    return {"subject": subject, "subject_type": "疾病", "relation": relation, "object": obj, "object_type": object_type, "source_document_id": doc_id, "source_text": source_text}

def extract_record(record: dict) -> list[dict]:
    title, content, doc_id = clean(record.get("title","")), clean(record.get("content","")), clean(record.get("document_id",""))
    category_ids = record.get("category_ids", [])  # read for schema validation/context; not emitted in the requested triple schema
    if not title or not content or not doc_id or not isinstance(category_ids, list) or not category_ids: return []
    triples = []
    for sentence in sentences(content):
        for relation, patterns in RELATION_PATTERNS.items():
            if not any(re.search(p, sentence, re.I) for p in patterns): continue
            if relation == "常见症状": candidates = [(x,"症状") for x in terms_in(sentence, SYMPTOMS)]
            elif relation == "治疗": candidates = [(x,"药物") for x in terms_in(sentence, DRUGS)] + [(x,"治疗方法") for x in terms_in(sentence, TREATMENTS)]
            elif relation == "病因": candidates = [(x,"疾病") for x in terms_in(sentence, CAUSE_TERMS)]
            elif relation == "不良反应": candidates = [(x,"症状") for x in terms_in(sentence, SYMPTOMS)]
            else: candidates = [(x,"检查方法") for x in terms_in(sentence, EXAMS)]
            for obj, obj_type in candidates:
                if obj and obj != title: triples.append(make_triple(title, relation, obj, obj_type, doc_id, sentence))
    return triples

def extract(records: list[dict]) -> list[dict]:
    seen, output = set(), []
    for record in records:
        for triple in extract_record(record):
            key = tuple(triple[k] for k in ("subject","subject_type","relation","object","object_type"))
            if key not in seen and all(triple.values()): seen.add(key); output.append(triple)
    return output

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract medical triples with auditable rules")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    records = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(records, list): raise ValueError("input JSON must be a list of documents")
    triples = extract(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(triples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Extracted {len(triples)} unique triples from {len(records)} documents")
    print(f"Saved to {args.output}")
    return 0

if __name__ == "__main__": raise SystemExit(main())
