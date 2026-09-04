"""CNKI adapter for authorized RefWorks/TXT exports.

The adapter intentionally does not read browser cookies, solve CAPTCHAs, or
reverse-engineer private endpoints. The user exports search results through
their authorized CNKI session; this adapter automatically parses, validates,
deduplicates, checkpoints, and publishes those batches.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..errors import ConfigurationError, InputRequiredError, ParseError
from ..models import Category, CollectionPage, SourceRecord
from ..utils import (
    canonicalize_url,
    normalize_doi,
    normalize_text,
    query_declares_clc_code,
    sha256_file,
    stable_fingerprint,
    utc_now,
)
from .base import SourceAdapter


BATCH_COLUMNS = {"batch_id", "file", "category_id", "query_text", "access_basis", "rights_statement"}
TAG_PATTERN = re.compile(r"^([A-Z][A-Z0-9]{1,3})(?:[ \t]{1,3}(.*))?[ \t]*$", re.IGNORECASE)
CLC_PATTERN = re.compile(r"(?<![A-Z])R(?:-?[0-9][0-9A-Z.+/-]*)", re.IGNORECASE)
KNOWN_REFWORKS_TAGS = {
    "RT", "SR", "A1", "A2", "A3", "A4", "AU", "AD", "T1", "T2", "TI",
    "JF", "JO", "YR", "PY", "FD", "IS", "OP", "K1", "KW", "AB", "N2",
    "SN", "CN", "LA", "DS", "DO", "DI", "UL", "UR", "LK", "ID", "CL",
    "CLC", "VO", "VL", "SP", "EP", "PB", "PP", "CY", "M1", "M3", "ER",
}


@dataclass(frozen=True)
class BatchSpec:
    batch_id: str
    path: Path
    category_id: str
    query_text: str
    access_basis: str
    rights_statement: str


class CNKIExportAdapter(SourceAdapter):
    name = "cnki"
    version = "1.2"
    capabilities = frozenset({"authorized-export", "refworks-txt", "offline", "resumable"})

    def __init__(
        self,
        *,
        input_dir: Path,
        batch_manifest: Path,
        known_category_ids: set[str],
        category_clc_codes: dict[str, str] | None = None,
        default_access_basis: str,
        default_rights_statement: str,
    ) -> None:
        self.input_dir = input_dir.resolve()
        self.batch_manifest = batch_manifest.resolve()
        self.known_category_ids = known_category_ids
        self.category_clc_codes = dict(category_clc_codes or {})
        self.default_access_basis = normalize_text(default_access_basis)
        self.default_rights_statement = normalize_text(default_rights_statement)
        self._batch_specs: list[BatchSpec] | None = None
        self._category_cache: dict[str, list[SourceRecord]] = {}

    def healthcheck(self) -> dict[str, Any]:
        specs = self._load_batch_specs()
        if not specs:
            raise InputRequiredError(
                "知网批次清单为空。请按 docs/使用说明.md 从已授权的知网会话导出 RefWorks/TXT，"
                "放入 data/inbox/cnki，并填写 batches.csv。"
            )
        missing = [str(spec.path) for spec in specs if not spec.path.is_file()]
        if missing:
            raise InputRequiredError("批次清单中的导出文件不存在: " + ", ".join(missing))
        return {
            **self.public_descriptor(),
            "input_dir": str(self.input_dir),
            "batch_manifest": str(self.batch_manifest),
            "batch_count": len(specs),
            "file_count": len({spec.path for spec in specs}),
        }

    def resume_identity(self) -> dict[str, Any]:
        return {
            **self.public_descriptor(),
            "input_dir": str(self.input_dir),
            "batch_manifest": str(self.batch_manifest),
        }

    def collect_page(self, category: Category, cursor: str | None, limit: int) -> CollectionPage:
        if limit <= 0:
            raise ConfigurationError("collect_page limit 必须大于 0")
        records = self._records_for_category(category.category_id)
        offset = _decode_cursor(cursor, records)
        if offset < 0 or offset > len(records):
            raise ParseError(f"知网检查点游标越界: {offset}/{len(records)}")
        page_records = records[offset : offset + limit]
        next_offset = offset + len(page_records)
        exhausted = next_offset >= len(records)
        return CollectionPage(
            records=page_records,
            next_cursor=_encode_cursor(records, next_offset),
            exhausted=exhausted,
            raw_count=len(page_records),
        )

    def _records_for_category(self, category_id: str) -> list[SourceRecord]:
        if category_id in self._category_cache:
            return self._category_cache[category_id]
        records: list[SourceRecord] = []
        for spec in self._load_batch_specs():
            if spec.category_id != category_id:
                continue
            file_hash = sha256_file(spec.path)
            text, encoding = read_cnki_text(spec.path)
            parsed = parse_refworks_records(text, source_name=spec.path.name)
            for index, tags in enumerate(parsed, start=1):
                records.append(
                    refworks_to_source_record(
                        tags,
                        spec=spec,
                        record_index=index,
                        file_hash=file_hash,
                        encoding=encoding,
                    )
                )
        self._category_cache[category_id] = records
        return records

    def _load_batch_specs(self) -> list[BatchSpec]:
        if self._batch_specs is not None:
            return self._batch_specs
        if not self.input_dir.exists() or not self.batch_manifest.exists():
            raise InputRequiredError(
                "未找到知网导出目录或 batches.csv。先阅读 docs/使用说明.md，"
                "再把 RefWorks/TXT 导出放入 data/inbox/cnki。"
            )
        with self.batch_manifest.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = BATCH_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ParseError(f"知网 batches.csv 缺少字段: {', '.join(sorted(missing))}")
            specs: list[BatchSpec] = []
            batch_ids: set[str] = set()
            for row_number, row in enumerate(reader, start=2):
                batch_id = normalize_text(row["batch_id"])
                filename = normalize_text(row["file"])
                category_id = normalize_text(row["category_id"])
                query_text = normalize_text(row["query_text"])
                if not batch_id or not filename or not category_id or not query_text:
                    raise ParseError(f"知网 batches.csv 第 {row_number} 行有空的必填字段")
                if batch_id in batch_ids:
                    raise ParseError(f"知网 batch_id 重复: {batch_id}")
                if category_id not in self.known_category_ids:
                    raise ParseError(f"知网 batches.csv 第 {row_number} 行引用未知类别: {category_id}")
                expected_code = self.category_clc_codes.get(category_id)
                if expected_code and not query_declares_clc_code(query_text, expected_code):
                    raise ParseError(
                        f"知网 batches.csv 第 {row_number} 行 query_text 必须包含精确目标号 "
                        f"{expected_code}"
                    )
                path = (self.input_dir / filename).resolve()
                if not _is_within(path, self.input_dir):
                    raise ParseError(f"知网导出文件不能位于 inbox 之外: {filename}")
                specs.append(
                    BatchSpec(
                        batch_id=batch_id,
                        path=path,
                        category_id=category_id,
                        query_text=query_text,
                        access_basis=normalize_text(row["access_basis"]) or self.default_access_basis,
                        rights_statement=normalize_text(row["rights_statement"]) or self.default_rights_statement,
                    )
                )
                batch_ids.add(batch_id)
        self._batch_specs = specs
        return specs


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _cursor_prefix(records: list[SourceRecord], offset: int) -> str:
    return stable_fingerprint(
        [
            {
                "source_record_id": record.source_record_id,
                "raw_hash": record.raw_hash,
                "raw_locator": record.raw_locator,
                "batch_id": record.batch_id,
                "query_text": record.query_text,
                "access_basis": record.access_basis,
                "rights_statement": record.rights_statement,
            }
            for record in records[:offset]
        ]
    )


def _encode_cursor(records: list[SourceRecord], offset: int) -> str:
    return json.dumps(
        {"offset": offset, "consumed_prefix": _cursor_prefix(records, offset)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_cursor(cursor: str | None, records: list[SourceRecord]) -> int:
    if cursor is None:
        return 0
    try:
        payload = json.loads(cursor)
        offset = int(payload["offset"])
        expected_prefix = str(payload["consumed_prefix"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ParseError("无效或过期的知网检查点游标；请使用新的 run") from exc
    if offset < 0 or offset > len(records):
        raise ParseError(f"知网检查点游标越界: {offset}/{len(records)}")
    actual_prefix = _cursor_prefix(records, offset)
    if actual_prefix != expected_prefix:
        raise ParseError(
            "知网已消费批次被修改、删除或重排。为防止漏数/错数，"
            "只允许在 batches.csv 末尾追加新批次；当前输入请使用新的 run。"
        )
    return offset


def read_cnki_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(f"无法识别知网导出编码（仅支持 UTF-8/GB18030）: {path.name}")
    probe = text[:5000].lower()
    if "<html" in probe or "<!doctype html" in probe or "验证码" in probe or "captcha" in probe:
        raise ParseError(f"{path.name} 看起来是登录/验证码 HTML，不是 RefWorks/TXT 导出")
    return text, encoding


def parse_refworks_records(text: str, *, source_name: str = "<memory>") -> list[dict[str, list[str]]]:
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    last_tag: str | None = None

    def flush() -> None:
        nonlocal current, last_tag
        if current:
            records.append(current)
        current = {}
        last_tag = None

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line
        if line.strip() == "ER":
            flush()
            continue
        candidate = TAG_PATTERN.match(line)
        match = candidate if candidate and candidate.group(1).upper() in KNOWN_REFWORKS_TAGS else None
        if match:
            tag, value = match.group(1).upper(), normalize_text(match.group(2) or "")
            if tag == "RT" and current:
                flush()
            if tag == "ER":
                flush()
                continue
            if value:
                current.setdefault(tag, []).append(value)
                last_tag = tag
            else:
                last_tag = None
        elif line.strip() and last_tag and current.get(last_tag):
            current[last_tag][-1] = normalize_text(current[last_tag][-1] + " " + line)
    flush()
    if not records:
        raise ParseError(f"{source_name} 中没有发现 RefWorks 记录；每条记录应以 'RT ' 开头")
    if not any(_first(record, "T1", "TI") for record in records):
        raise ParseError(f"{source_name} 中没有任何 T1/TI 标题，可能不是知网 RefWorks 格式")
    return records


def refworks_to_source_record(
    tags: dict[str, list[str]],
    *,
    spec: BatchSpec,
    record_index: int,
    file_hash: str,
    encoding: str,
) -> SourceRecord:
    title = _first(tags, "T1", "TI")
    abstract = _first(tags, "AB", "N2")
    doi = normalize_doi(_first(tags, "DO", "DI"))
    source_url = canonicalize_url(_first(tags, "UL", "UR", "LK"))
    authors = _split_repeated(tags, ("A1", "AU"), separators=";；")
    keywords = _split_repeated(tags, ("K1", "KW"), separators=";；,")
    journal = _first(tags, "JF", "JO", "T2")
    year_match = re.search(r"(?:19|20)\d{2}", _first(tags, "YR", "PY", "FD"))
    year = int(year_match.group(0)) if year_match else None
    explicit_id = _first(tags, "ID", "CNKI")
    if explicit_id:
        source_record_id = "id:" + normalize_text(explicit_id)
    elif doi:
        source_record_id = "doi:" + doi
    elif source_url:
        source_record_id = "url:" + source_url
    else:
        signature = "\0".join([normalize_text(title), authors[0] if authors else "", str(year or ""), journal])
        source_record_id = "derived:" + stable_fingerprint(signature).split(":", 1)[1]
    clc_values = []
    for tag in ("CL", "CLC"):
        clc_values.extend(tags.get(tag, []))
    source_clc_codes = sorted({match.group(0).upper() for value in clc_values for match in CLC_PATTERN.finditer(value)})
    record_hash = stable_fingerprint(tags)
    return SourceRecord(
        source_name="cnki",
        source_record_id=source_record_id,
        title=title,
        abstract=abstract,
        content=abstract,
        source_url=source_url,
        authors=authors,
        keywords=keywords,
        journal=journal,
        publication_year=year,
        doi=doi,
        language=_first(tags, "LA") or "zh",
        source_clc_codes=source_clc_codes,
        raw_locator=f"{spec.path.name}#record={record_index}",
        raw_hash=record_hash,
        batch_id=spec.batch_id,
        query_text=spec.query_text,
        access_basis=spec.access_basis,
        rights_statement=spec.rights_statement,
        retrieved_at=utc_now(),
        raw_metadata={
            "export_file": spec.path.name,
            "export_file_sha256": file_hash,
            "export_encoding": encoding,
            "record_index": record_index,
            "refworks_tags": tags,
        },
    )


def _first(tags: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = tags.get(name, [])
        for value in values:
            normalized = normalize_text(value)
            if normalized:
                return normalized
    return ""


def _split_repeated(tags: dict[str, list[str]], names: Iterable[str], *, separators: str) -> list[str]:
    values: list[str] = []
    pattern = f"[{re.escape(separators)}]"
    for name in names:
        for raw in tags.get(name, []):
            values.extend(normalize_text(item) for item in re.split(pattern, raw))
    return list(dict.fromkeys(value for value in values if value))
