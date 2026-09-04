# -*- coding: utf-8 -*-
"""临时：用训练好的 NER 模型对 medical_sample.json 随机取 10 条文本做预测，
结果写入 UTF-8 文件查看效果。"""
import json
import random
import sys
from pathlib import Path

# 保证能 import 到项目根目录下的 extraction 包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import deep_learning_layer as dl  # noqa: E402

RAW = ROOT / "data" / "raw" / "medical_sample.json"
OUT = ROOT / "data" / "_infer_result.md"
N_SAMPLE = 10


def _fmt(spans):
    """把 (term, type, conf) 列表格式化成易读字符串。"""
    if not spans:
        return "无"
    return " │ ".join(f"{term}[{etype}]({conf:.2f})" for term, etype, conf in spans)


def main():
    records = json.loads(RAW.read_text(encoding="utf-8"))
    random.seed(0)
    picks = random.sample(records, min(N_SAMPLE, len(records)))

    tokenizer, model = dl._get_model()
    lines = []
    for rec in picks:
        title = (rec.get("title") or "").strip()
        lines.append(f"## {rec.get('document_id')}  《{title}》")
        if title:
            lines.append("  标题实体: " + _fmt(dl._predict_sentence(tokenizer, model, title)))
        # 正文取前 3 句做预测，避免输出过长
        for sent in dl._sentences_of(rec)[:3]:
            if not sent:
                continue
            spans = dl._predict_sentence(tokenizer, model, sent)
            if spans:
                lines.append(f"  - {sent}")
                lines.append(f"    实体: {_fmt(spans)}")
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"done: 已采样 {len(picks)} 条 -> {OUT}")


if __name__ == "__main__":
    main()