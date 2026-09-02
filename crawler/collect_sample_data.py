#!/usr/bin/env python3
"""Collect a public medical text corpus for the MedGraph project.

The script uses Wikimedia's public MediaWiki API (no login required).  It only
requests search results and plain-text page extracts; it does not bypass any
captcha, access control, or robots policy.  Search results are paged so that
each category can collect up to ``--per-category`` documents.
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
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://zh.wikipedia.org/w/api.php"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/raw/medical_sample.json"
USER_AGENT = "MedGraph-sample-crawler/1.0 (educational project; contact via Wikimedia policy)"
REQUEST_DELAY = 0.3

# One hundred disease/topic-level categories under Chinese Library
# Classification R (medicine & health), each mapped to Chinese search terms
# that return article pages on the Chinese Wikipedia.  The category IDs are
# stable IDs used by our project.
CATEGORY_TERMS: dict[str, list[str]] = {
    # 预防医学 / 基础医学 / 中医 (R1/R2/R3)
    "public_health_epidemiology": ["流行病学", "传染病流行病学", "疫情"],
    "public_health_nutrition": ["营养学", "膳食指南", "营养素"],
    "public_health_health_education": ["健康教育", "健康素养", "健康促进"],
    "public_health_screening": ["医学筛查", "健康体检", "癌症筛查"],
    "public_health_first_aid": ["急救", "心肺复苏", "创伤急救"],
    "tcm_acupuncture": ["针灸", "针刺", "穴位"],
    "tcm_herbal_medicine": ["中药", "中药材", "方剂", "中草药"],
    "tcm_theory": ["中医", "阴阳", "五行", "辨证论治"],
    "basic_anatomy": ["人体解剖学", "解剖学"],
    "basic_physiology": ["生理学", "人体生理学"],
    "basic_immunology": ["免疫学", "免疫系统", "免疫应答"],
    "geriatrics_medicine": ["老年医学", "老年病学"],
    # 呼吸系统 (R56)
    "respiratory_asthma": ["哮喘", "支气管哮喘"],
    "respiratory_copd": ["慢性阻塞性肺病", "慢阻肺", "肺气肿"],
    "respiratory_pneumonia": ["肺炎", "细菌性肺炎", "支原体肺炎"],
    "respiratory_tuberculosis": ["肺结核", "结核病"],
    "respiratory_lung_cancer": ["肺癌", "非小细胞肺癌", "小细胞肺癌"],
    "respiratory_sleep_apnea": ["睡眠呼吸暂停", "睡眠呼吸障碍"],
    # 心血管 (R54)
    "cardiovascular_hypertension": ["高血压", "原发性高血压"],
    "cardiovascular_coronary": ["冠心病", "冠状动脉粥样硬化", "心绞痛"],
    "cardiovascular_myocardial_infarction": ["心肌梗死", "急性心肌梗死"],
    "cardiovascular_heart_failure": ["心力衰竭", "慢性心力衰竭", "急性心力衰竭", "肺水肿", "心源性休克"],
    "cardiovascular_arrhythmia": ["心律失常", "心房颤动", "心动过速"],
    "cardiovascular_atherosclerosis": ["动脉粥样硬化", "动脉硬化", "高脂血症", "血栓", "血管狭窄"],
    # 消化 (R57)
    "digestive_gastritis": ["胃炎", "慢性胃炎", "萎缩性胃炎"],
    "digestive_peptic_ulcer": ["消化性溃疡", "胃溃疡", "十二指肠溃疡", "幽门螺杆菌", "上消化道出血", "十二指肠炎"],
    "digestive_hepatitis": ["肝炎", "乙型肝炎", "病毒性肝炎"],
    "digestive_cirrhosis": ["肝硬化", "脂肪肝", "门静脉高压", "肝纤维化", "肝功能衰竭", "腹水", "肝性脑病"],
    "digestive_pancreatitis": ["胰腺炎", "急性胰腺炎", "慢性胰腺炎", "胆囊炎", "胆结石", "胆总管结石", "胆囊结石", "胰腺"],
    # 内分泌代谢 / 血液 / 肾泌尿 / 风湿免疫 (R58/R55/R59)
    "endocrine_diabetes": ["糖尿病", "1型糖尿病", "2型糖尿病"],
    "endocrine_thyroid_disease": ["甲状腺功能亢进症", "甲状腺功能减退症", "甲亢"],
    "endocrine_osteoporosis": ["骨质疏松症", "骨质疏松"],
    "endocrine_gout": ["痛风", "高尿酸血症", "代谢综合征"],
    "hematology_anemia": ["贫血", "缺铁性贫血", "巨幼细胞性贫血"],
    "hematology_leukemia": ["白血病", "急性白血病", "慢性粒细胞白血病"],
    "hematology_lymphoma": ["淋巴瘤", "霍奇金淋巴瘤"],
    "hematology_hemophilia": ["血友病", "凝血功能障碍"],
    "nephrology_nephritis": ["肾小球肾炎", "肾病综合征", "肾炎"],
    "nephrology_kidney_failure": ["肾衰竭", "慢性肾脏病", "尿毒症", "急性肾损伤", "透析", "肾性贫血"],
    "nephrology_uti": ["尿路感染", "膀胱炎", "尿道炎"],
    "nephrology_kidney_stone": ["肾结石", "尿路结石"],
    "rheumatology_rheumatoid_arthritis": ["类风湿关节炎", "类风湿性关节炎"],
    "rheumatology_sle": ["系统性红斑狼疮", "红斑狼疮"],
    "rheumatology_ankylosing_spondylitis": ["强直性脊柱炎"],
    "rheumatology_osteoarthritis": ["骨关节炎", "退行性关节炎"],
    # 神经 / 精神 (R74)
    "neurology_stroke": ["脑卒中", "中风", "脑梗死", "脑出血"],
    "neurology_alzheimer": ["阿尔茨海默病", "老年痴呆"],
    "neurology_parkinson": ["帕金森病", "帕金森氏症"],
    "neurology_epilepsy": ["癫痫", "癫痫发作"],
    "neurology_migraine": ["偏头痛", "头痛"],
    "neurology_multiple_sclerosis": ["多发性硬化", "脱髓鞘疾病"],
    "psychiatric_depression": ["抑郁症", "抑郁障碍"],
    "psychiatric_anxiety": ["焦虑症", "广泛性焦虑障碍", "惊恐障碍", "社交焦虑障碍", "恐惧症", "强迫症"],
    "psychiatric_schizophrenia": ["精神分裂症"],
    "psychiatric_insomnia": ["失眠", "失眠症", "睡眠障碍", "嗜睡症", "睡眠卫生", "时差"],
    # 传染病 (R51)
    "infectious_influenza": ["流行性感冒", "流感", "甲型流感"],
    "infectious_covid19": ["2019冠状病毒病", "新型冠状病毒", "COVID-19", "严重急性呼吸综合征", "新冠后遗症", "奥密克戎"],
    "infectious_aids": ["艾滋病", "HIV"],
    "infectious_dengue": ["登革热", "登革病毒"],
    "infectious_rabies": ["狂犬病", "狂犬病毒"],
    # 肿瘤 (R73)
    "oncology_breast_cancer": ["乳腺癌", "乳腺肿瘤"],
    "oncology_gastric_cancer": ["胃癌", "胃肿瘤"],
    "oncology_liver_cancer": ["肝癌", "肝细胞癌", "肝肿瘤", "胆管癌", "转移性肝癌"],
    "oncology_cervical_cancer": ["宫颈癌", "子宫颈癌"],
    "oncology_prostate_cancer": ["前列腺癌"],
    # 外科 / 骨科 (R6/R68)
    "surgery_appendicitis": ["阑尾炎", "急性阑尾炎"],
    "surgery_hernia": ["疝气", "腹股沟疝", "脐疝", "食管裂孔疝", "切口疝", "疝修补术"],
    "surgery_burn": ["烧伤", "烫伤"],
    "surgery_organ_transplant": ["器官移植", "肾移植", "肝移植"],
    "surgery_varicose_veins": ["静脉曲张", "下肢静脉曲张", "大隐静脉", "静脉功能不全", "血栓性静脉炎"],
    "orthopedics_fracture": ["骨折", "股骨颈骨折", "桡骨远端骨折"],
    "orthopedics_disc_herniation": ["腰椎间盘突出", "椎间盘突出", "坐骨神经痛"],
    "orthopedics_scoliosis": ["脊柱侧弯", "脊柱侧凸"],
    # 妇产 / 儿科 (R71/R72)
    "gynecology_uterine_fibroids": ["子宫肌瘤"],
    "gynecology_endometriosis": ["子宫内膜异位症"],
    "gynecology_pcos": ["多囊卵巢综合征", "卵巢囊肿", "高雄激素血症", "排卵障碍", "多毛症"],
    "obstetrics_pregnancy": ["妊娠", "怀孕", "分娩", "产前检查"],
    "pediatrics_neonatal_jaundice": ["新生儿黄疸", "黄疸"],
    "pediatrics_hand_foot_mouth": ["手足口病"],
    "pediatrics_autism": ["自闭症", "孤独症", "自闭症谱系障碍"],
    "pediatrics_adhd": ["注意缺陷多动障碍", "多动症", "ADHD"],
    # 皮肤 / 耳鼻喉 / 眼科 / 口腔 (R75-R78)
    "dermatology_eczema": ["湿疹", "特应性皮炎"],
    "dermatology_psoriasis": ["银屑病", "牛皮癣"],
    "dermatology_acne": ["痤疮", "青春痘"],
    "dermatology_herpes_zoster": ["带状疱疹", "水痘"],
    "ent_otitis_media": ["中耳炎", "分泌性中耳炎"],
    "ent_rhinitis": ["鼻炎", "过敏性鼻炎", "鼻窦炎", "鼻息肉", "鼻中隔偏曲", "慢性鼻炎"],
    "ent_pharyngitis": ["咽炎", "扁桃体炎", "喉炎", "扁桃体肥大", "腺样体肥大", "声带息肉"],
    "ophthalmology_cataract": ["白内障"],
    "ophthalmology_glaucoma": ["青光眼"],
    "ophthalmology_myopia": ["近视", "屈光不正"],
    "stomatology_dental_caries": ["龋齿", "蛀牙"],
    "stomatology_periodontitis": ["牙周炎", "牙龈炎"],
    "stomatology_oral_ulcer": ["口腔溃疡"],
    # 药学 / 康复 / 老年
    "pharmacology_antibiotics": ["抗生素", "抗菌药物", "青霉素"],
    "pharmacology_analgesics": ["镇痛药", "布洛芬", "阿司匹林", "对乙酰氨基酚"],
    "pharmacology_vaccine": ["疫苗", "免疫接种"],
    "pharmacology_adr": ["药物不良反应", "药物相互作用", "过敏反应"],
    "rehabilitation_therapy": ["康复治疗", "物理治疗", "作业治疗"],
    "geriatrics_fall_prevention": ["老年人跌倒", "跌倒预防"],
}


def api_get(params: dict[str, Any], retries: int = 5) -> dict[str, Any]:
    query = urlencode({**params, "format": "json", "formatversion": "2"})
    request = Request(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed public API URL
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if attempt + 1 >= retries:
                break
            if exc.code == 429:  # rate limited: respect the server's Retry-After hint
                retry_after = 5
                try:
                    retry_after = int(exc.headers.get("Retry-After", "5"))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(retry_after, 30) + 1)
            else:
                time.sleep(2.0 * (attempt + 1))
        except Exception as exc:  # network failures should be retried, then reported clearly
            last_error = exc
            if attempt + 1 >= retries:
                break
            time.sleep(2.0 * (attempt + 1))
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
    """Return up to `limit` article dicts matching any of `terms`, paging through search results."""
    page_ids: list[str] = []
    offset = 0
    while len(page_ids) < limit:
        result = api_get({
            "action": "query",
            "list": "search",
            "srsearch": " OR ".join(terms),
            "srnamespace": 0,
            "srlimit": min(50, limit - len(page_ids)),
            "sroffset": offset,
        })
        hits = result.get("query", {}).get("search", [])
        total_hits = result.get("query", {}).get("searchinfo", {}).get("totalhits") or 0
        for hit in hits:
            page_id = str(hit.get("pageid", ""))
            if page_id and page_id not in page_ids:
                page_ids.append(page_id)
        next_offset = result.get("continue", {}).get("sroffset")
        if not hits or not next_offset or next_offset <= offset or (total_hits and len(page_ids) >= total_hits):
            break
        offset = next_offset
        time.sleep(REQUEST_DELAY)  # polite pacing between search result pages
    page_ids = page_ids[:limit]
    if not page_ids:
        return []
    pages: list[dict[str, Any]] = []
    for start in range(0, len(page_ids), 20):
        result = api_get({
            "action": "query",
            "pageids": "|".join(page_ids[start:start + 20]),
            "prop": "extracts|info",
            "explaintext": 1,
            "exintro": 0,
            "inprop": "url",
        })
        pages.extend(result.get("query", {}).get("pages", []))
        if start + 20 < len(page_ids):
            time.sleep(REQUEST_DELAY)  # polite pacing between extract batches
    return pages


def collect(limit: int, per_category: int, categories: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    collected: list[dict[str, Any]] = []
    failed_categories: list[str] = []
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()
    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    items = list(CATEGORY_TERMS.items())
    if categories:
        items = [(category_id, CATEGORY_TERMS[category_id]) for category_id in categories]
    for index, (category_id, terms) in enumerate(items, start=1):
        category_count = 0
        try:
            # Fetch slightly more pages than the target so that duplicate removal
            # and short-text filtering do not leave the category short.
            for page in search_pages(terms, per_category + 10):
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
                time.sleep(REQUEST_DELAY)  # polite pacing for a public API
        except RuntimeError as exc:
            print(f"[{index:3d}/{len(items)}] {category_id}: FAILED ({exc})")
            failed_categories.append(category_id)
            continue
        print(f"[{index:3d}/{len(items)}] {category_id}: {category_count} documents")
    return collected, failed_categories


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public medical text corpus from Wikimedia Wikipedia API")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of documents (default: per-category x category count)")
    parser.add_argument("--per-category", type=int, default=30, help="maximum documents per category (default: 30)")
    parser.add_argument("--categories", type=str, default=None, help="comma-separated category ids to crawl (default: all)")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between API requests (default: 0.3)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output JSON path")
    args = parser.parse_args()
    if args.per_category <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("--limit and --per-category must be positive")
    if args.delay < 0:
        parser.error("--delay must be non-negative")
    global REQUEST_DELAY
    REQUEST_DELAY = args.delay
    selected: list[str] | None = None
    if args.categories:
        selected = [category_id.strip() for category_id in args.categories.split(",") if category_id.strip()]
        unknown = [category_id for category_id in selected if category_id not in CATEGORY_TERMS]
        if unknown:
            parser.error(f"unknown category ids: {', '.join(unknown)}")
    expected_categories = selected or list(CATEGORY_TERMS)
    limit = args.limit if args.limit is not None else args.per_category * len(expected_categories)
    try:
        records, failed_categories = collect(limit, args.per_category, categories=selected)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    records = records[: limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    per_category_counts: dict[str, int] = {}
    for record in records:
        for category_id in record["category_ids"]:
            per_category_counts[category_id] = per_category_counts.get(category_id, 0) + 1
    counts = sorted(per_category_counts.values())
    print(f"Collected {len(records)} documents across {len(per_category_counts)} categories")
    if counts:
        print(f"Per-category counts: min={counts[0]}, max={counts[-1]}, avg={sum(counts) / len(counts):.1f}")
    empty_categories = [category_id for category_id in expected_categories if per_category_counts.get(category_id, 0) == 0]
    if empty_categories:
        print(f"WARNING: {len(empty_categories)} categories have no documents: {', '.join(empty_categories)}")
    if failed_categories:
        print(f"WARNING: {len(failed_categories)} categories failed: {', '.join(failed_categories)}")
    print(f"Saved to {args.output}")

    if failed_categories:
        print("ERROR: some categories failed to collect", file=sys.stderr)
        return 2
    if empty_categories:
        print("ERROR: at least one category has no documents", file=sys.stderr)
        return 2
    if len(records) < limit:
        print(f"ERROR: only collected {len(records)} documents; target is {limit}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
