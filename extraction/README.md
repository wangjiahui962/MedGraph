# 医学文本信息抽取

`extract_triples.py` 是一个不依赖大模型 API 的规则抽取模块。它读取 `data/raw/medical_sample.json`，使用小型医学词典与关系触发词，在句子范围内识别疾病、药物、症状、治疗方法和检查方法，输出带原文证据的结构化三元组。

运行：

```bash
python3 extraction/extract_triples.py
```

默认输入为 `data/raw/medical_sample.json`，输出为 `data/processed/triples.json`。也可用 `--input` 和 `--output` 指定路径。输出会按主体、主体类型、关系、客体、客体类型去重，并过滤空值；规则和词典均在脚本顶部显式列出，便于后续扩展。
