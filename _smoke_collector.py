# -*- coding: utf-8 -*-
"""冒烟脚本：用注入的假 transport 验证 collector 端到端（采集→门禁→发布→导入 documents.db）。
用后即删。不访问真实网络、不污染真实 documents.db（导入到临时库）。"""
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, r"e:\研一\暑期作业\知识图谱构建项目\MedGraph")

from collector.agent import CollectionAgent  # noqa: E402
from collector.catalog import load_catalog, select_categories  # noqa: E402
from collector.config import CollectionSettings  # noqa: E402
from collector.importer import import_published_generation  # noqa: E402
from collector.sources.mediawiki import MediaWikiAdapter  # noqa: E402
from collector.storage import StateStore  # noqa: E402

ROOT = Path(r"e:\研一\暑期作业\知识图谱构建项目\MedGraph")
TMP_DB = ROOT / "collector" / "data" / "_smoke_documents.db"


def fake_transport(url, headers, timeout, max_bytes):
    """伪造 MediaWiki API 响应：搜索 + 详情 两阶段。"""
    query = parse_qs(urlsplit(url).query)
    if "list" in query and query["list"] == ["search"]:
        # 搜索：返回一个命中，页面标题/内容包含查询词（肺疾病），保证 normalize 接受
        return {
            "query": {
                "search": [
                    {"pageid": 9001, "snippet": "肺疾病", "titlesnippet": "肺疾病科普"},
                ]
            }
        }
    return {
        "query": {
            "pages": [
                {
                    "pageid": 9001,
                    "title": "肺疾病科普",
                    "extract": "肺疾病是一类常见疾病。肺疾病的早期诊断非常重要，"
                               "本条目介绍肺疾病的预防与治疗知识，供医学文本抽取使用。",
                    "fullurl": "https://zh.wikipedia.org/wiki/肺疾病科普",
                    "revisions": [{"revid": 11, "timestamp": "2026-01-01T00:00:00Z"}],
                }
            ]
        }
    }


def main() -> int:
    settings = CollectionSettings.load(ROOT / "collector" / "configs" / "collection.json")
    settings.apply_overrides(min_categories=1, min_documents=1, min_per_category=1, page_size=5)
    categories = select_categories(load_catalog(settings.catalog_path), ["clc_r563"])
    adapter = MediaWikiAdapter(
        api_url="https://zh.wikipedia.org/w/api.php",
        user_agent="MedGraphSmokeTestBot/1.0 (https://example.com/medgraph; educational smoke test)",
        request_delay=0,
        transport=fake_transport,
        sleep=lambda _: None,
    )
    store = StateStore(settings.state_db)
    agent = CollectionAgent(
        settings=settings,
        categories=categories,
        adapter=adapter,
        store=store,
        per_category=2,
    )
    result = agent.run(run_id="smoke_win_endtoend_002", dry_run=False)
    print("run:", result.status, result.message)
    print("metrics:", json.dumps(result.metrics, ensure_ascii=False)[:300])
    assert result.status == "SUCCEEDED", "采集应发布成功"
    assert result.generation_dir is not None
    documents_path = result.generation_dir / "documents.jsonl"
    imported = import_published_generation(documents_path, db_path=TMP_DB)
    print("imported_to_documents_db:", imported)
    assert imported >= 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
