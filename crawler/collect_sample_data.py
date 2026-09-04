#!/usr/bin/env python3
"""Collect a small, reproducible sample of public medical texts.

The script uses Wikimedia's public MediaWiki API (no login required).  It only
requests search results and plain-text page extracts; it does not bypass any
captcha, access control, or robots policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://zh.wikipedia.org/w/api.php"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/raw/medical_sample.json"
USER_AGENT = "MedGraph-sample-crawler/1.0 (educational project; contact via Wikimedia policy)"

# Eight categories, with broad terms that return article pages rather than a
# single narrow disease.  The category IDs are stable IDs used by our project.
CATEGORY_TERMS: dict[str, list[str]] = {
    "respiratory": ["支气管哮喘", "肺炎", "慢性阻塞性肺病", "肺结核", "呼吸系统疾病", "肺癌", "睡眠呼吸暂停"],
    "cardiovascular": ["高血压", "冠心病", "心肌梗死", "心力衰竭", "心律失常", "动脉粥样硬化", "心脏病"],
    "digestive": ["胃炎", "消化性溃疡", "肝炎", "脂肪肝", "肝硬化", "结直肠癌", "胰腺炎"],
    "endocrine": ["糖尿病", "甲状腺功能亢进症", "甲状腺功能减退症", "骨质疏松症", "痛风", "代谢综合征"],
    "neurology": ["脑卒中", "阿尔茨海默病", "帕金森病", "癫痫", "偏头痛", "抑郁症", "焦虑症"],
    "infectious": ["流行性感冒", "2019冠状病毒病", "艾滋病", "病毒性肝炎", "登革热", "麻疹"],
    "oncology": ["乳腺癌", "肺癌", "胃癌", "肝癌", "白血病", "前列腺癌", "宫颈癌"],
    "public_health": ["疫苗", "营养学", "公共卫生", "健康教育", "医学筛查", "急救", "传染病"],
}


def api_get(params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    query = urlencode({**params, "format": "json", "formatversion": "2"})
    request = Request(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed public API URL
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network failures should be retried, then reported clearly
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Wikimedia API request failed after {retries} attempts: {last_error}")


def clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value


def quality_score(title: str, content: str) -> float:
    # Transparent heuristic for a sample: complete title plus a useful extract.
    score = 0.55
    if len(title) >= 2:
        score += 0.15
    if len(content) >= 180:
        score += 0.25
    elif len(content) >= 80:
        score += 0.15
    return round(min(score, 0.99), 2)


def search_pages(terms: list[str], limit: int) -> list[dict[str, Any]]:
    result = api_get({
        "action": "query",
        "list": "search",
        "srsearch": " OR ".join(terms),
        "srnamespace": 0,
        "srlimit": limit,
    })
    page_ids = [str(hit.get("pageid")) for hit in result.get("query", {}).get("search", []) if hit.get("pageid")]
    if not page_ids:
        return []
    pages = api_get({
        "action": "query",
        "pageids": "|".join(page_ids),
        "prop": "extracts|info",
        "explaintext": 1,
        "exintro": 0,
        "inprop": "url",
    }).get("query", {}).get("pages", [])
    return pages


def collect(limit: int, per_category: int) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()
    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for category_id, terms in CATEGORY_TERMS.items():
        category_count = 0
        pages = search_pages(terms, min(len(terms), per_category + 2))
        for page in pages:
            if len(collected) >= limit or category_count >= per_category:
                break
            title = clean_text(page.get("title", ""))
            content = clean_text(page.get("extract", ""))
            source_url = clean_text(page.get("fullurl", ""))
            if not title or not content or not source_url or len(content) < 80:
                continue
            content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content_hash in seen_hashes or source_url in seen_urls:
                continue
            seen_hashes.add(content_hash)
            seen_urls.add(source_url)
            collected.append({
                "document_id": f"doc_{len(collected) + 1:06d}",
                "category_ids": [category_id],
                "title": title,
                "content": content,
                "source_url": source_url,
                "license": "public-info",
                "collected_at": collected_at,
                "content_hash": content_hash,
                "quality_score": quality_score(title, content),
            })
            category_count += 1
            time.sleep(0.25)  # polite pacing for a public API
    return collected


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public medical text samples from Wikimedia Wikipedia API")
    parser.add_argument("--limit", type=int, default=50, help="maximum number of documents (default: 50)")
    parser.add_argument("--per-category", type=int, default=7, help="maximum documents per category (default: 7)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output JSON path")
    args = parser.parse_args()
    if args.limit <= 0 or args.per_category <= 0:
        parser.error("--limit and --per-category must be positive")
    try:
        records = collect(args.limit, args.per_category)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if len(records) < min(args.limit, 50):
        print(f"ERROR: only collected {len(records)} valid documents; target is {min(args.limit, 50)}", file=sys.stderr)
        return 2
    records = records[: args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Collected {len(records)} documents across {len({c for r in records for c in r['category_ids']})} categories")
    print(f"Saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
