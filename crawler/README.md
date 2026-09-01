# 医学样本文本采集

`collect_sample_data.py` 使用 Wikimedia/Wikipedia 中文站的公开 MediaWiki API，抓取约 50 条无需登录的医学卫生文章摘要。脚本不会绕过验证码、访问控制或网站限制，并以较低频率请求接口。

在项目根目录运行：

```bash
python3 crawler/collect_sample_data.py
```

默认结果写入 `data/raw/medical_sample.json`。可用参数调整数量或输出路径：

```bash
python3 crawler/collect_sample_data.py --limit 50 --per-category 7 \
  --output data/raw/medical_sample.json
```

输出记录包含 `document_id`、`category_ids`、`title`、`content`、`source_url`、`license`、`collected_at`、`content_hash` 和 `quality_score`。脚本会清洗空白字符、过滤短文本，并按正文哈希和来源 URL 去重；若无法达到目标数量会返回非零退出码。

数据来自公开百科页面，`license` 字段按本项目数据清单标记为 `public-info`；正式发布前应根据来源页面的具体许可协议补充合规说明。
