"""Small deterministic helpers shared by adapters, storage, and exporters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"spm", "from", "source", "campaign", "ref"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return re.sub(r"\s+", " ", text).strip()


def normalize_doi(value: str) -> str:
    doi = normalize_text(value).lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi\s*:\s*", "", doi)
    return doi.strip()


def query_declares_clc_code(query_text: str, clc_code: str) -> bool:
    """Match one exact CLC code without treating a child code as its parent."""

    query = normalize_text(query_text).upper()
    code = normalize_text(clc_code).strip("[]{}").upper()
    return bool(
        code
        and re.search(
            rf"(?<![A-Z0-9.]){re.escape(code)}(?![A-Z0-9.])",
            query,
        )
    )


def canonicalize_url(value: str) -> str:
    url = normalize_text(value)
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        return url
    clean_query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower.startswith(TRACKING_QUERY_PREFIXES) or key_lower in TRACKING_QUERY_KEYS:
            continue
        clean_query.append((key, val))
    clean_query.sort()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(clean_query), ""))


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)


def stable_document_id(source_name: str, stable_record_id: str) -> str:
    digest = hashlib.sha256(f"{source_name}\0{stable_record_id}".encode("utf-8")).hexdigest()
    return "doc_" + digest[:24]


def split_terms(value: str) -> tuple[str, ...]:
    return tuple(term for term in (normalize_text(item) for item in value.split("|")) if term)


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
