# -*- coding: utf-8 -*-
"""【三元组清洗】去除主/客（subject / object）选词明显错误的三元组。

背景：LLM 抽取层有时会把“泛称词 / 学科名 / 书名 / 论著 / 理论名”当成医疗实体，
或者把整句/列表切进 subject / object，形成错误三元组。典型例子：
    - “中医外科学”被标成 Disease，并产生 30 条 RELATED_TO；
    - “表里辨证”一文把 表证 / 里证（证候名）当 Disease；
    - object 直接是 症状/药物/治疗/检查 这类泛称，而不是具体实体；
    - object 是一串用顿号并列的多个概念（“受限的、重复的行为模式、兴趣或活动”）。

本脚本用一组可配置的规则找出这类三元组：
    1. generic     —— subject/object 是泛称/范畴词（症状、药物、治疗、检查…）；
    2. suffix      —— subject 是“XX学 / XX书 / XX典 / XX辨证”这类学科/典籍名，却被当作医疗实体；
    3. doc_title   —— subject 直接等于文章标题，且标题本身是学科/理论/典籍/机构名（非疾病名）；
    4. fragment    —— object 含顿号并列（多个概念被并进一个实体槽，Drug 类型除外）；
    5. review      —— 读取人工审查清单 data/processed/review_list.json 中 review=="reject" 的 id；
    6. tcm_syndrome(可选) —— 把中医证候名（表证/里证/寒证/热证/虚证…）当 Disease 的三元组。

安全设计：
    - 默认只做 DRY RUN：打印统计并把每条候选（含命中规则、证据句）写入报告 JSON；
    - --apply 才真正删除：先备份 triples.db（生成 *.bak_时间戳），删除后自动重导出
      data/processed/triples.json，保证前端与 DB 一致。

用法：
    python db/clean_triples.py                       # ① 干跑：预览将被删除的候选
    python db/clean_triples.py --top 10              #    只打印前 10 条示例（报告仍写全量）
    python db/clean_triples.py --apply               # ② 确认：备份 + 删除 + 重导出前端 JSON
    python db/clean_triples.py --rules generic,suffix,doc_title,fragment,review
                                                    #    只启用指定规则
    python db/clean_triples.py --also tcm_syndrome   #    额外启用“中医证候当疾病”规则
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRIPLES_DB = ROOT / "data" / "triples.db"
DOCUMENTS_DB = ROOT / "data" / "documents.db"
REVIEW_LIST = ROOT / "data" / "processed" / "review_list.json"
DEFAULT_REPORT = ROOT / "data" / "processed" / "clean_report.json"

# ---------------------------------------------------------------------------
# 规则配置（可直接按自己的语料修改/扩充）
# ---------------------------------------------------------------------------

# 规则 1：作为“临床实体”出现时仍只是泛称/范畴词的词
GENERIC_TOKENS = {
    "中医", "西医", "中西医", "蒙医", "藏医",
    "疾病", "症状", "药物", "治疗", "检查", "疗法", "并发症",
    "患者", "病人", "本病", "该病", "该药", "该法",
    "本书", "本文", "该书", "该文", "文章", "书中", "以上", "以下", "上述",
}
# 只有被标成这些“具体临床类型”时才判定为泛称误用（避免误伤 Population 等合法使用）
CLINICAL_TYPES = {
    "Disease", "Symptom", "Drug", "Treatment",
    "Examination", "RiskFactor", "Complication", "Department",
}

# 规则 2：学科/典籍名后缀（作为 subject 出现且被标成临床实体时视为选词错误）
DISCIPLINE_SUFFIXES = (
    "学", "医学", "科学", "辨证", "书", "典", "谱",
)
# 规则 2 生效的实体类型（覆盖 Disease/Drug/Treatment…；不含普通 RELATED_TO 兜底名词使用）
DISCIPLINE_CHECK_TYPES = {
    "Disease", "Symptom", "Drug", "Treatment",
    "Examination", "RiskFactor", "Complication",
}

# 规则 3：文章标题本身看起来不像一个“疾病/药物”实体（学科、理论、典籍、机构等）时，
# subject==title 的三元组视为把文章主题当成了医疗实体
NON_MEDICAL_TITLE_SUFFIXES = (
    "学", "医学", "中医", "西医", "辨证", "书", "典", "论", "经", "谱",
    "学报", "杂志", "期刊", "简报", "综述", "指南", "大纲", "概论", "列表",
    "大学", "学院", "医院", "中心", "学部", "研究所", "学会", "基金会",
)

# 规则 4 不检查的类型：含顿号也可能是一个合法专名（如“伤寒、副伤寒甲、乙三联菌苗”是 Drug）
FRAGMENT_SKIP_TYPES = {"Drug"}

# 规则 6（可选）：中医“证候/辨证”术语被当成 Disease（按项目本体 Disease 指具体疾病，证候不属于）
TCM_SYNDROMES = {
    "表证", "里证", "寒证", "热证", "虚证", "实证", "阴证", "阳证",
    "表寒证", "表热证", "里寒证", "里热证", "虚寒证", "虚热证",
    "气虚", "血虚", "阴虚", "阳虚", "气血两虚", "阴阳两虚",
    "八纲辨证", "阴阳辨证", "表里辨证", "寒热辨证", "虚实辨证",
    "脏腑辨证", "经络辨证", "六经辨证", "卫气营血辨证", "三焦辨证",
}

# 默认启用的规则（review 规则在 review_list.json 存在时自动生效）
DEFAULT_RULES = ["generic", "suffix", "doc_title", "fragment", "review"]
OPTIONAL_RULES = ["tcm_syndrome"]

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def load_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        sys.exit(f"找不到三元组库：{db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, subject, subject_type, relation, object, object_type, "
        "source_document_id, source_text, confidence, layer FROM triples"
    )]
    conn.close()
    return rows


def load_titles(db_path: Path) -> dict[str, str]:
    """doc_xxxxx -> 标题（用于 doc_title 规则）。文档库缺失时返回空 dict。"""
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        titles = {
            doc_id: (title or "")
            for doc_id, title in conn.execute(
                "SELECT document_id, title FROM documents"
            )
        }
    except sqlite3.OperationalError:
        titles = {}
    finally:
        conn.close()
    return titles


def load_review_rejects(path: Path) -> set[tuple]:
    """读取人工审查清单中 review=="reject" 的条目。

    返回 (id, subject, relation, object, source_document_id) 五元组集合：
    要求 id 与实体/关系/来源完全一致才删除，避免库重导后 id 漂移导致误删。
    """
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, list):
        return set()
    rejects: set[tuple] = set()
    for r in data:
        if not isinstance(r, dict) or r.get("review") != "reject":
            continue
        if r.get("id") is None or not r.get("subject") or not r.get("object"):
            continue
        rejects.add((
            int(r["id"]), r["subject"], r["relation"], r["object"],
            r.get("source_document_id"),
        ))
    return rejects


def is_clinical(value_type: str, clinical_types: set[str]) -> bool:
    return value_type in clinical_types


# ---------------------------------------------------------------------------
# 规则判定
# ---------------------------------------------------------------------------


def check_rules(
    row: dict[str, Any],
    titles: dict[str, str],
    review_rejects: set[int],
    enabled: set[str],
) -> list[str]:
    """返回该三元组命中的全部规则 id（空列表 = 干净）。"""
    reasons: list[str] = []
    subject, s_type = (row["subject"] or "").strip(), row["subject_type"]
    object_, o_type = (row["object"] or "").strip(), row["object_type"]

    if "generic" in enabled:
        if subject in GENERIC_TOKENS and is_clinical(s_type, CLINICAL_TYPES):
            reasons.append("generic")
        elif object_ in GENERIC_TOKENS and is_clinical(o_type, CLINICAL_TYPES):
            reasons.append("generic")

    if "suffix" in enabled:
        if subject and s_type in DISCIPLINE_CHECK_TYPES and subject.endswith(DISCIPLINE_SUFFIXES):
            reasons.append("suffix")

    if "doc_title" in enabled:
        title = titles.get(row["source_document_id"], "")
        if (
            subject and title
            and subject == title
            and title.endswith(NON_MEDICAL_TITLE_SUFFIXES)
        ):
            reasons.append("doc_title")

    if "fragment" in enabled:
        if object_ and o_type not in FRAGMENT_SKIP_TYPES and "、" in object_:
            reasons.append("fragment")

    if "review" in enabled:
        reject_key = (
            row["id"], row["subject"], row["relation"], row["object"],
            row["source_document_id"],
        )
        if reject_key in review_rejects:
            reasons.append("review")

    if "tcm_syndrome" in enabled:
        if subject in TCM_SYNDROMES and s_type == "Disease":
            reasons.append("tcm_syndrome")

    return reasons


# ---------------------------------------------------------------------------
# 报告 / 删除
# ---------------------------------------------------------------------------


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        reasons = row["_reasons"]
        if reasons:
            for r in reasons:
                stats[r] += 1
            candidates.append(row)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_total": len(rows),
        "candidate_total": len(candidates),
        "stats": dict(stats),
        "rows": [
            {
                "id": r["id"],
                "subject": r["subject"],
                "subject_type": r["subject_type"],
                "relation": r["relation"],
                "object": r["object"],
                "object_type": r["object_type"],
                "source_document_id": r["source_document_id"],
                "source_text": r["source_text"],
                "rules": r["_reasons"],
            }
            for r in candidates
        ],
    }


def delete_rows(db_path: Path, ids: list[int]) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"DELETE FROM triples WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main() -> int:
    # 保证管道/终端下中文不乱码（Windows 控制台代码页兼容）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=TRIPLES_DB, help="三元组库路径")
    parser.add_argument("--rules", type=str, default=",".join(DEFAULT_RULES),
                        help="启用规则（逗号分隔），可选：" + ",".join(sorted(set(DEFAULT_RULES + OPTIONAL_RULES))))
    parser.add_argument("--also", type=str, default="", help="追加启用可选规则，如 tcm_syndrome")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="报告 JSON 路径")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只干跑预览）")
    parser.add_argument("--top", type=int, default=0, help="终端里打印前 N 条候选（0=全部不打印）")
    args = parser.parse_args()

    rules = {r.strip() for r in args.rules.split(",") if r.strip()}
    if args.also:
        rules |= {r.strip() for r in args.also.split(",") if r.strip()}
    unknown = rules - set(DEFAULT_RULES + OPTIONAL_RULES)
    if unknown:
        sys.exit(f"未知规则：{sorted(unknown)}")

    rows = load_rows(args.db)
    titles = load_titles(DOCUMENTS_DB)
    review_rejects = load_review_rejects(REVIEW_LIST)

    for row in rows:
        row["_reasons"] = check_rules(row, titles, review_rejects, rules)

    report = build_report(rows)
    report["rules"] = sorted(rules)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"候选明细已写入 -> {args.report}")

    candidates = report["rows"]
    if "review" in rules:
        print(f"\n审查清单（review_list.json）中 reject 条目：{len(review_rejects)}"
              f"，与当前库匹配：{report['stats'].get('review', 0)}（不匹配的旧 id 自动跳过）")
    print(f"\n库内三元组总数：{report['db_total']}")
    print(f"命中候选（将被删除，若 --apply）：{report['candidate_total']}")
    for rule, n in report["stats"].items():
        print(f"  - {rule:14s} {n}")

    if args.top and candidates:
        shown = candidates[: args.top]
        print(f"\n示例（前 {len(shown)} 条）：")
        for c in shown:
            print(
                f"  #{c['id']} [{','.join(c['rules'])}] "
                f"{c['subject']}({c['subject_type']}) -{c['relation']}-> "
                f"{c['object']}({c['object_type']})  {c['source_document_id']}"
            )
            print(f"      证据: {(c['source_text'] or '')[:60]}")

    if not args.apply:
        print("\n[DRY RUN] 未修改数据库。确认无误后加 --apply 执行删除。")
        return 0

    if not candidates:
        print("没有候选需要删除。")
        return 0

    ids = [c["id"] for c in candidates]
    backup = args.db.with_name(f"{args.db.name}.bak_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(args.db, backup)
    deleted = delete_rows(args.db, ids)
    print(f"\n已备份 -> {backup}")
    print(f"已删除 {deleted} 条三元组，库内剩余 {report['db_total'] - deleted} 条。")

    # 删除后与前端数据保持同步（复用 db/store_triples.py 的导出逻辑）
    try:
        from db import store_triples
        remaining = store_triples.export_triples_json()
        print(f"已重导出前端数据 -> data/processed/triples.json（{remaining} 条）")
    except Exception as exc:  # 导出失败不应阻断删除流程
        print(f"WARN: 重导出前端 JSON 失败：{exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
