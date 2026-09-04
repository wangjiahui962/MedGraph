"""Canonical data contracts used by every source adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


SCHEMA_VERSION = "1.0"
# Bump this whenever normalization, validation, dedupe, or orchestration semantics
# change in a way that makes resuming an older run unsafe.
PIPELINE_VERSION = "1.3"


class RunStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    ACQUIRING = "ACQUIRING"
    NORMALIZING = "NORMALIZING"
    DEDUPING = "DEDUPING"
    VALIDATING = "VALIDATING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    ACTIVATING = "ACTIVATING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_DRY_RUN = "SUCCEEDED_DRY_RUN"
    COMPLETED_WITH_GAPS = "COMPLETED_WITH_GAPS"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    FAILED = "FAILED"


TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.SUCCEEDED_DRY_RUN.value,
    RunStatus.FAILED.value,
}


@dataclass(frozen=True)
class Category:
    category_id: str
    clc_code: str
    clc_name: str
    parent_code: str
    query_terms: tuple[str, ...]
    target_count: int
    enabled: bool
    authority_source: str
    reviewed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "clc_code": self.clc_code,
            "clc_name": self.clc_name,
            "parent_code": self.parent_code,
            "query_terms": list(self.query_terms),
            "target_count": self.target_count,
            "enabled": self.enabled,
            "authority_source": self.authority_source,
            "reviewed": self.reviewed,
        }


@dataclass
class SourceRecord:
    source_name: str
    source_record_id: str
    title: str
    abstract: str = ""
    content: str = ""
    source_url: str = ""
    authors: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    journal: str = ""
    publication_year: Optional[int] = None
    doi: str = ""
    language: str = "zh"
    source_clc_codes: list[str] = field(default_factory=list)
    raw_locator: str = ""
    raw_hash: str = ""
    batch_id: str = ""
    query_text: str = ""
    access_basis: str = ""
    rights_statement: str = ""
    retrieved_at: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectionPage:
    records: list[SourceRecord]
    next_cursor: Optional[str]
    exhausted: bool
    raw_count: int
