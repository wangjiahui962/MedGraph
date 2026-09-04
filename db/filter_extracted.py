# -*- coding: utf-8 -*-
"""把 extraction/extract.py 刚产出的 triples_extracted.json 按关键词规则过滤。

作用：在“提取现有数据”流水线里，入库前先用与 db/clean_triples.py 相同的关键词规则
（泛称词 / 学科后缀 / 文章标题误用 / 顿号并列片段 / 中医证候当疾病）剔除坏三元组，
再交给 db/store_triples.py 入库，避免坏数据进入 triples.db 后还要人工再清。

用法：
    python db/filter_extracted.py                  # 就地过滤 data/processed/triples_extracted.json
    python db/filter_extracted.py --input xx.json --output yy.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.clean_triples import (  # noqa: E402
    DOCUMENTS_DB,
    DEFAULT_RULES,
    OPTIONAL_RULES,
    check_rules,
    load_titles,
)

# 入库前的流水线过滤：review 规则依赖库里已有 id，对刚抽出的新批次不适用，故排除；
# 其余关键词规则 + 中医证候（此前人工清洗时也启用了 tcm_syndrome）默认全开。
DEFAULT_FILTER_RULES = "generic,suffix,doc_title,fragment,tcm_syndrome"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "data" / "processed" / "triples_extracted.json")
    parser.add_argument("--output", type=Path, default=None,
                        help="过滤结果输出路径（默认就地覆盖 input）")
    parser.add_argument("--rules", type=str, default=DEFAULT_FILTER_RULES,
                        help="启用规则，逗号分隔；可选：" + ",".join(sorted(set(DEFAULT_RULES + OPTIONAL_RULES))))
    args = parser.parse_args()
    output = args.output or args.input

    rules = {r.strip() for r in args.rules.split(",") if r.strip()}
    unknown = rules - set(DEFAULT_RULES + OPTIONAL_RULES)
    if unknown:
        print(f"ERROR: 未知规则：{sorted(unknown)}", file=sys.stderr)
        return 1

    if not args.input.is_file():
        print(f"WARN: 未找到输入文件 {args.input}，跳过关键词过滤。", file=sys.stderr)
        return 0
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: 无法解析 {args.input}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(rows, list):
        print(f"ERROR: {args.input} 必须是三元组列表。", file=sys.stderr)
        return 1

    titles = load_titles(DOCUMENTS_DB)
    stats: Counter[str] = Counter()
    kept: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        reasons = check_rules(row, titles, set(), rules)
        if reasons:
            for rule in reasons:
                stats[rule] += 1
        else:
            kept.append(row)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"输入 {len(rows)} 条，关键词过滤后剩 {len(kept)} 条"
          f"（移除 {len(rows) - len(kept)} 条）-> {output}")
    if stats:
        for rule, n in stats.items():
            print(f"  - {rule:14s} 移除 {n} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
