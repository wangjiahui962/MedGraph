# -*- coding: utf-8 -*-
"""从中文维基百科搜索新增文章，繁体转简体后写入 documents.db（Web“增加新数据”的后端）。

流程：关键词搜索 → 取页面引言正文（exintro）→ normalize + OpenCC 繁体转简体 →
     按 (source_name, pageid) 生成稳定 document_id → 只入库尚未存在的文章。

特点：
    - 不走 collector 的 4400 篇门禁，适合在 Web 上“一次增加几篇新文章”；
    - document_id 复用 collector 的 stable_document_id("mediawiki", pageid)，
      与后续批量采集同源不重复；
    - 每次只新增 documents.db 中没有的页面（增量、幂等）；
    - 入库的是简体（与抽取/前端口径一致）。

运行：
    python -m collector.wiki_add --count 5
    python -m collector.wiki_add --count 3 --dry-run    # 只打印候选，不入库
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.utils import canonicalize_url, normalize_text, sha256_text, stable_document_id, utc_now  # noqa: E402
from db import store_documents  # noqa: E402

try:
    from opencc import OpenCC as _OpenCC
    _T2S = _OpenCC("t2s")
except Exception:  # pragma: no cover - opencc 缺失时保留原文
    _T2S = None

CONFIG_FILE = ROOT / "collector" / "configs" / "collection.json"
CATALOG_FILE = ROOT / "collector" / "configs" / "clc_r_categories.csv"
DEFAULT_DELAY = 0.5


def _t2s(value: str) -> str:
    """繁体 -> 简体（opencc t2s）；未安装 opencc 时原样返回。"""
    if _T2S is None:
        return value
    try:
        return _T2S.convert(value)
    except Exception:  # pragma: no cover
        return value


def _config_section() -> dict[str, Any]:
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return (data.get("sources") or {}).get("mediawiki") or {}


def _catalog_queries() -> list[tuple[str, str]]:
    """返回 [(category_id, query_term)]，仅取启用且已审校的 R 类医学类目。"""
    rows: list[tuple[str, str]] = []
    if not CATALOG_FILE.is_file():
        return rows
    with CATALOG_FILE.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if str(row.get("enabled", "")).strip().lower() != "true":
                continue
            if str(row.get("reviewed", "")).strip().lower() != "true":
                continue
            cat_id = (row.get("category_id") or "").strip()
            terms = [
                t.strip()
                for t in (row.get("query_terms") or "").split("|")
                if t.strip()
            ]
            for term in terms:
                if cat_id and term:
                    rows.append((cat_id, term))
    return rows


def _api_get(
    params: dict[str, Any],
    *,
    api_url: str,
    user_agent: str,
    timeout: float,
    retries: int,
    delay: float,
) -> dict[str, Any]:
    """调用 MediaWiki action=query，返回 JSON；网络/限流失败抛异常。"""
    query = dict(params)
    query.setdefault("format", "json")
    url = f"{api_url}?{urlencode(query)}"
    last_error: Exception | None = None
    for attempt in range(retries):
        request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(8_000_000)
            data = json.loads(payload.decode("utf-8"))
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data
        except HTTPError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(delay * (attempt + 2))
                continue
            raise
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(delay * (attempt + 1))
                continue
            raise
        except RuntimeError:
            raise
    raise RuntimeError(f"请求失败：{last_error}")


def _search_pageids(term: str, *, api_url: str, user_agent: str, timeout: float, retries: int, delay: float, limit: int) -> list[tuple[str, str]]:
    data = _api_get(
        {
            "action": "query",
            "list": "search",
            "srsearch": term,
            "srnamespace": 0,
            "srlimit": str(limit),
        },
        api_url=api_url, user_agent=user_agent, timeout=timeout, retries=retries, delay=delay,
    )
    hits = data.get("query", {}).get("search") or []
    return [
        (str(hit.get("pageid")), str(hit.get("title") or ""))
        for hit in hits
        if isinstance(hit, dict) and hit.get("pageid") and str(hit.get("title") or "").strip()
    ]


def _fetch_extracts(
    titles: list[str],
    *,
    api_url: str,
    user_agent: str,
    timeout: float,
    retries: int,
    delay: float,
) -> dict[str, dict[str, Any]]:
    """一次请求取若干页面的引言正文 + URL；返回 {pageid: {...}}。"""
    data = _api_get(
        {
            "action": "query",
            "prop": "extracts|info",
            "explaintext": "1",
            "exintro": "1",
            "inprop": "url",
            "redirects": "1",
            "titles": "\n".join(titles),
        },
        api_url=api_url, user_agent=user_agent, timeout=timeout, retries=retries, delay=delay,
    )
    pages = data.get("query", {}).get("pages") or {}
    result: dict[str, dict[str, Any]] = {}
    if isinstance(pages, list):
        items = pages
    elif isinstance(pages, dict):
        items = pages.values()
    else:
        items = []
    for page in items:
        if not isinstance(page, dict) or not str(page.get("pageid", "")).isdigit():
            continue
        result[str(page["pageid"])] = page
    return result


def build_record(page: dict[str, Any], *, category_id: str, rights: str) -> dict[str, Any]:
    """把 MediaWiki 页面转成 documents.db 扁平记录（正文/标题已繁体转简体）。"""
    title = _t2s(normalize_text(page.get("title")))
    content = _t2s(normalize_text(page.get("extract")))
    page_id = str(page["pageid"])
    return {
        "document_id": stable_document_id("mediawiki", page_id),
        "category_ids": [category_id] if category_id else [],
        "title": title,
        "content": content,
        "source_url": canonicalize_url(page.get("fullurl") or ""),
        "license": rights or "Wikipedia CC BY-SA（教学研究本地使用）",
        "collected_at": utc_now(),
        "content_hash": sha256_text(content),
        "quality_score": 0.9,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    mediawiki = _config_section()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=5, help="本次最多新增几篇（默认 5）")
    parser.add_argument("--api-url", default=mediawiki.get("api_url", "https://zh.wikipedia.org/w/api.php"))
    parser.add_argument("--user-agent", default=mediawiki.get("user_agent") or "MedGraphWebAddBot/0.1 (course project)")
    parser.add_argument("--timeout", type=float, default=float(mediawiki.get("timeout", 20)))
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--dry-run", action="store_true", help="只打印将新增的候选，不入库")
    args = parser.parse_args()
    count = max(1, min(args.count, 100))

    queries = list(OrderedDict.fromkeys((cat, term) for cat, term in _catalog_queries()))
    if not queries:
        print("ERROR: 没有可用的检索词（catalog 读取失败或为空）。", file=sys.stderr)
        return 2

    conn = store_documents._connect(store_documents.DEFAULT_DB)
    existing = {row[0] for row in conn.execute("SELECT document_id FROM documents")}
    known_titles = {row[0] for row in conn.execute("SELECT title FROM documents")}
    conn.close()

    records: list[dict[str, Any]] = []
    failures: list[str] = []
    consecutive_failures = 0
    pending_ids: set[str] = set()
    added = 0
    rights = mediawiki.get("rights_statement", "")

    for category_id, term in queries:
        if added >= count:
            break
        try:
            hits = _search_pageids(
                term, api_url=args.api_url, user_agent=args.user_agent,
                timeout=args.timeout, retries=args.retries, delay=args.delay, limit=8,
            )
        except Exception as exc:  # 单个检索词失败不阻断后续，但连续失败过多则提前退出
            failures.append(f"{term}: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= 5:
                print(f"WARN: 连续 {consecutive_failures} 个检索词请求失败，提前结束（请检查网络）。", file=sys.stderr)
                break
            continue
        consecutive_failures = 0
        time.sleep(args.delay)
        wanted: list[tuple[str, str]] = []
        for page_id, title in hits:
            doc_id = stable_document_id("mediawiki", page_id)
            if doc_id in existing or doc_id in pending_ids:
                continue
            if title in known_titles:
                continue
            wanted.append((page_id, title))
            pending_ids.add(doc_id)
            if len(wanted) >= 2:
                break
        if not wanted:
            continue
        try:
            pages = _fetch_extracts(
                [title for _, title in wanted],
                api_url=args.api_url, user_agent=args.user_agent,
                timeout=args.timeout, retries=args.retries, delay=args.delay,
            )
        except Exception as exc:
            failures.append(f"{term}: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= 5:
                print(f"WARN: 连续 {consecutive_failures} 个检索词请求失败，提前结束（请检查网络）。", file=sys.stderr)
                break
            continue
        for page_id, _ in wanted:
            if added >= count:
                break
            page = pages.get(page_id)
            if not page or not str(page.get("extract") or "").strip():
                continue
            content = normalize_text(page.get("extract"))
            if len(content) < 80:
                continue
            doc_id = stable_document_id("mediawiki", page_id)
            record = build_record(page, category_id=category_id, rights=rights)
            if record["document_id"] != doc_id:
                continue
            if record["title"] in known_titles or record["content_hash"] in {
                r["content_hash"] for r in records
            }:
                continue
            records.append(record)
            added += 1
            print(f"  候选 [{added}] {doc_id} {record['title']}（{category_id}）", flush=True)

    if args.dry_run:
        print(f"DRY-RUN 候选 {len(records)} 篇（未入库）。")
        return 0

    if not records:
        if failures:
            print(f"ERROR: 未能新增任何文章（{len(failures)} 个检索词失败）。", file=sys.stderr)
            for msg in failures[-5:]:
                print(f"  - {msg}", file=sys.stderr)
        else:
            print("当前没有可新增的候选（检索词均命中已有文章）。")
        return 2 if failures else 0

    imported = store_documents.import_records(records, update=False)
    print(f"ADDED:{imported} 已新增 {imported} 篇维基文章（目标 {count} 篇）。")
    if failures:
        print(f"WARN: 另有 {len(failures)} 个检索词请求失败，可在网络恢复后重试。", file=sys.stderr)
        for msg in failures[:3]:
            print(f"  - {msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
