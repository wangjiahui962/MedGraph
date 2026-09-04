"""Source-independent normalization, validation, relevance, and dedupe keys."""

from __future__ import annotations

import re
from typing import Any

from .models import PIPELINE_VERSION, SCHEMA_VERSION, Category, SourceRecord
from .utils import (
    canonicalize_url,
    normalize_doi,
    normalize_text,
    query_declares_clc_code,
    sha256_text,
    stable_document_id,
    unique_preserving_order,
    utc_now,
)


class RejectedCandidate(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_candidate(
    record: SourceRecord,
    category: Category,
    *,
    run_id: str,
    collector_version: str,
    min_text_chars: int,
) -> tuple[dict[str, Any], list[str]]:
    title = normalize_text(record.title)
    abstract = normalize_text(record.abstract)
    content = normalize_text(record.content or abstract)
    if not title:
        raise RejectedCandidate("missing_title")
    if len(content) < min_text_chars:
        raise RejectedCandidate("text_too_short")
    if not record.source_name or not record.source_record_id:
        raise RejectedCandidate("missing_source_identity")
    if not record.access_basis:
        raise RejectedCandidate("missing_access_basis")

    search_evidence = []
    if record.source_name == "mediawiki":
        search_evidence = [
            normalize_text(record.raw_metadata.get("search_title_snippet")),
            normalize_text(record.raw_metadata.get("search_snippet")),
        ]
    combined = normalize_text(
        " ".join(
            [
                title,
                content,
                *(normalize_text(keyword) for keyword in record.keywords),
                *search_evidence,
            ]
        )
    ).lower()
    matched_terms = [
        term
        for term in category.query_terms
        if _query_term_matches(term, combined, allow_medical_stem=record.source_name == "mediawiki")
    ]
    mediawiki_ranked_mapping = (
        record.source_name == "mediawiki"
        and not matched_terms
        and _is_ranked_medical_search_result(title, combined, record.raw_metadata)
    )
    source_codes = [normalize_clc_code(code) for code in record.source_clc_codes]
    source_codes = [code for code in source_codes if code]
    if record.source_name == "cnki" and not query_declares_clc_code(
        record.query_text,
        category.clc_code,
    ):
        raise RejectedCandidate("cnki_query_missing_target_clc_code")
    verified = any(_clc_matches(category.clc_code, code) for code in source_codes)
    if source_codes and not any(_clc_compatible(category.clc_code, code) for code in source_codes):
        raise RejectedCandidate("source_clc_conflict")
    if record.source_name == "mediawiki" and not matched_terms and not mediawiki_ranked_mapping:
        raise RejectedCandidate("query_terms_not_found")
    if record.source_name == "cnki" and not verified and not matched_terms:
        raise RejectedCandidate("cnki_unverified_no_term_match")

    if verified:
        assignment_basis = "source_metadata"
        classification_confidence = 0.98
    elif record.source_name == "cnki":
        assignment_basis = "cnki_batch_query_mapping"
        classification_confidence = 0.80 if matched_terms else 0.72
    elif mediawiki_ranked_mapping:
        assignment_basis = "mediawiki_ranked_search_mapping"
        classification_confidence = 0.45
    else:
        assignment_basis = "query_term_match"
        classification_confidence = 0.62

    doi = normalize_doi(record.doi)
    source_url = canonicalize_url(record.source_url)
    source_record_id = normalize_text(record.source_record_id)
    document_id = stable_document_id(record.source_name, source_record_id)
    content_hash = sha256_text(content)
    relevance_score = _relevance_score(record.source_name, matched_terms, title, content, verified)
    quality_score = _quality_score(title, content, bool(record.authors), relevance_score)
    collected_at = record.retrieved_at or utc_now()

    classification = {
        "category_id": category.category_id,
        "scheme": "CLC",
        "clc_code": category.clc_code,
        "clc_name": category.clc_name,
        "assignment_basis": assignment_basis,
        "verified": verified,
        "confidence": classification_confidence,
        "evidence": _classification_evidence(record, category, mediawiki_ranked_mapping),
        "source_clc_codes": source_codes,
    }
    provenance = {
        "run_id": run_id,
        "batch_id": record.batch_id,
        "query_text": record.query_text,
        "raw_locator": record.raw_locator,
        "raw_hash": record.raw_hash,
        "collected_at": collected_at,
        "collector": record.source_name,
        "collector_version": collector_version,
        "pipeline_version": PIPELINE_VERSION,
    }
    document = {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "source": {
            "name": record.source_name,
            "record_id": source_record_id,
            "url": source_url,
            "retrieved_at": collected_at,
            "access_basis": normalize_text(record.access_basis),
            "rights_statement": normalize_text(record.rights_statement) or "unknown",
            "raw_locator": normalize_text(record.raw_locator),
        },
        "bibliography": {
            "title": title,
            "abstract": abstract,
            "authors": unique_preserving_order(normalize_text(author) for author in record.authors),
            "keywords": unique_preserving_order(normalize_text(keyword) for keyword in record.keywords),
            "journal": normalize_text(record.journal),
            "publication_year": record.publication_year,
            "doi": doi,
        },
        "text": {
            "content": content,
            "content_type": "abstract" if abstract and content == abstract else "fulltext",
            "language": normalize_text(record.language) or "zh",
            "content_hash": content_hash,
        },
        "classifications": [classification],
        "collection": {
            "run_id": run_id,
            "collector": record.source_name,
            "collector_version": collector_version,
            "pipeline_version": PIPELINE_VERSION,
            "query_id": f"{record.batch_id}:{category.category_id}",
        },
        "quality": {
            "status": "accepted",
            "score": quality_score,
            "relevance_score": relevance_score,
            "matched_terms": matched_terms,
            "reasons": ["unverified_ranked_search_mapping"] if mediawiki_ranked_mapping else [],
        },
        "provenance": [provenance],
        "extensions": {"source_metadata": record.raw_metadata},
    }
    errors = validate_document(document, {category.category_id: category})
    if errors:
        raise RejectedCandidate("schema:" + ";".join(errors))
    return document, dedupe_keys(document)


def validate_document(document: dict[str, Any], catalog: dict[str, Category]) -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported_schema_version")
    document_id = document.get("document_id")
    if not isinstance(document_id, str) or not re.fullmatch(r"doc_[0-9a-f]{24}", document_id):
        errors.append("invalid_document_id")
    source = document.get("source")
    if not isinstance(source, dict) or not source.get("name") or not source.get("record_id"):
        errors.append("invalid_source")
    bibliography = document.get("bibliography")
    if not isinstance(bibliography, dict) or not normalize_text(bibliography.get("title")):
        errors.append("missing_title")
    text = document.get("text")
    if not isinstance(text, dict) or not normalize_text(text.get("content")):
        errors.append("missing_content")
    elif text.get("content_hash") != sha256_text(normalize_text(text.get("content"))):
        errors.append("content_hash_mismatch")
    classifications = document.get("classifications")
    if not isinstance(classifications, list) or not classifications:
        errors.append("missing_classification")
    else:
        for assignment in classifications:
            if not isinstance(assignment, dict):
                errors.append("invalid_classification")
                continue
            category = catalog.get(str(assignment.get("category_id", "")))
            if category is None:
                errors.append("unknown_category")
            elif assignment.get("clc_code") != category.clc_code:
                errors.append("clc_code_mismatch")
            if not str(assignment.get("clc_code", "")).startswith("R"):
                errors.append("not_clc_r")
    provenance = document.get("provenance")
    if not isinstance(provenance, list) or not provenance:
        errors.append("missing_provenance")
    collection = document.get("collection")
    if not isinstance(collection, dict):
        errors.append("invalid_collection")
    elif collection.get("pipeline_version") != PIPELINE_VERSION:
        errors.append("unsupported_pipeline_version")
    return errors


def dedupe_keys(document: dict[str, Any]) -> list[str]:
    source = document["source"]["name"]
    keys = [f"{source}|record|{normalize_text(document['source']['record_id']).lower()}"]
    doi = normalize_doi(document["bibliography"].get("doi", ""))
    if doi:
        keys.append(f"{source}|doi|{doi}")
    url = canonicalize_url(document["source"].get("url", ""))
    if url:
        keys.append(f"{source}|url|{url}")
    keys.append(f"{source}|content|{document['text']['content_hash']}")
    return unique_preserving_order(keys)


def normalize_clc_code(value: str) -> str:
    return normalize_text(value).strip("[]{}").upper()


def _clc_matches(expected: str, actual: str) -> bool:
    expected_key = _clc_hierarchy_key(expected)
    actual_key = _clc_hierarchy_key(actual)
    return bool(expected_key and actual_key.startswith(expected_key))


def _clc_compatible(expected: str, actual: str) -> bool:
    expected_key = _clc_hierarchy_key(expected)
    actual_key = _clc_hierarchy_key(actual)
    return bool(
        expected_key
        and actual_key
        and (actual_key.startswith(expected_key) or expected_key.startswith(actual_key))
    )


def _clc_hierarchy_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_clc_code(value))


def _relevance_score(source: str, matched_terms: list[str], title: str, content: str, verified: bool) -> float:
    if verified:
        return 0.98
    score = 0.48 if source == "mediawiki" else 0.58
    title_lower = title.lower()
    for term in matched_terms:
        score += 0.20 if normalize_text(term).lower() in title_lower else 0.08
    if len(content) >= 200:
        score += 0.06
    return round(min(score, 0.95), 2)


def _quality_score(title: str, content: str, has_authors: bool, relevance: float) -> float:
    score = 0.25 + min(len(title) / 100, 0.15) + min(len(content) / 1200, 0.30)
    if has_authors:
        score += 0.10
    score += relevance * 0.20
    return round(min(score, 0.99), 2)


def _query_term_matches(term: str, combined: str, *, allow_medical_stem: bool) -> bool:
    normalized = normalize_text(term).lower()
    if not normalized:
        return False
    if normalized in combined:
        return True
    if not allow_medical_stem:
        return False
    stem = normalized
    for fragment in (
        "系统疾病",
        "性疾病",
        "疾病",
        "医学",
        "科学",
        "学科",
        "疗法",
        "治疗",
        "诊断",
        "检查",
    ):
        stem = stem.replace(fragment, "")
    stem = stem.removesuffix("学")
    return len(stem) >= 2 and stem in combined


def _is_ranked_medical_search_result(title: str, combined: str, metadata: dict[str, Any]) -> bool:
    try:
        rank = int(metadata.get("search_rank", 0))
    except (TypeError, ValueError):
        return False
    if rank <= 0 or rank > 100:
        return False
    blocked_title_markers = (
        "大学",
        "学院",
        "医院",
        "研究所",
        "研究院",
        "科学院",
        "学会",
        "协会",
        "委员会",
        "实验室",
    )
    if any(marker in title for marker in blocked_title_markers):
        return False
    medical_anchors = (
        "医学",
        "疾病",
        "健康",
        "卫生",
        "治疗",
        "诊断",
        "症状",
        "药物",
        "手术",
        "生理",
        "病理",
        "护理",
        "临床",
        "感染",
        "肿瘤",
        "免疫",
        "患者",
        "医生",
        "器官",
        "细胞",
        "基因",
        "病毒",
        "细菌",
        "流行病",
        "预防",
        "康复",
        "药理",
        "血液",
    )
    return sum(anchor in combined for anchor in medical_anchors) >= 2


def _classification_evidence(
    record: SourceRecord,
    category: Category,
    mediawiki_ranked_mapping: bool,
) -> str:
    base = record.query_text or " | ".join(category.query_terms)
    if not mediawiki_ranked_mapping:
        return base
    return f"{base}; mediawiki_search_rank={record.raw_metadata.get('search_rank')}"
