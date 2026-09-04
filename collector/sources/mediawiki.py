"""Public MediaWiki API adapter used as the second website implementation."""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from ..errors import ConfigurationError, ParseError, SourceError
from ..models import Category, CollectionPage, SourceRecord
from ..utils import canonicalize_url, normalize_text, stable_fingerprint, utc_now
from .base import SourceAdapter


Transport = Callable[[str, dict[str, str], float, int], dict[str, Any]]


class MediaWikiAdapter(SourceAdapter):
    name = "mediawiki"
    version = "1.3"
    capabilities = frozenset({"http", "public-api", "pagination", "resumable"})

    def __init__(
        self,
        *,
        api_url: str,
        user_agent: str,
        request_delay: float = 0.5,
        timeout: float = 20.0,
        retries: int = 4,
        max_lag: int = 5,
        max_candidates_per_category: int = 100,
        max_response_bytes: int = 8_000_000,
        rights_statement: str = "See the source page for license and attribution requirements.",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(api_url)
        if not _is_allowed_wikipedia_url(parsed):
            raise ConfigurationError("MediaWiki API 必须是 wikipedia.org 下的 HTTPS 地址")
        if (
            request_delay < 0
            or timeout <= 0
            or retries <= 0
            or max_lag <= 0
            or max_candidates_per_category <= 0
            or max_response_bytes <= 0
        ):
            raise ConfigurationError("MediaWiki 请求参数无效")
        self.api_url = api_url
        self.user_agent = normalize_text(user_agent)
        self.request_delay = request_delay
        self.timeout = timeout
        self.retries = retries
        self.max_lag = max_lag
        self.max_candidates_per_category = max_candidates_per_category
        self.max_response_bytes = max_response_bytes
        self.rights_statement = normalize_text(rights_statement)
        self.transport = transport or self._default_transport
        self.sleep = sleep

    def healthcheck(self) -> dict[str, Any]:
        if not self.user_agent or "contact" in self.user_agent.lower():
            raise ConfigurationError("请在 configs/collection.json 中把 MediaWiki User-Agent 改成真实项目标识")
        return {**self.public_descriptor(), "api_url": self.api_url}

    def resume_identity(self) -> dict[str, Any]:
        return {
            **self.public_descriptor(),
            "api_url": self.api_url,
            "user_agent": self.user_agent,
            "request_delay": self.request_delay,
            "timeout": self.timeout,
            "retries": self.retries,
            "max_lag": self.max_lag,
            "max_candidates_per_category": self.max_candidates_per_category,
            "max_response_bytes": self.max_response_bytes,
        }

    def collect_page(self, category: Category, cursor: str | None, limit: int) -> CollectionPage:
        if limit <= 0:
            raise ConfigurationError("collect_page limit 必须大于 0")
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise ParseError(f"无效的 MediaWiki 游标: {cursor}") from exc
        remaining_candidates = self.max_candidates_per_category - offset
        if remaining_candidates <= 0:
            return CollectionPage(records=[], next_cursor=str(offset), exhausted=True, raw_count=0)
        query = " OR ".join(category.query_terms)
        search_limit = min(limit, 50, remaining_candidates)
        search_data = self._api_get(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": 0,
                "srlimit": search_limit,
                "sroffset": offset,
            }
        )
        hits = search_data.get("query", {}).get("search", [])
        if not isinstance(hits, list):
            raise ParseError("MediaWiki 搜索响应缺少 query.search 列表")
        page_ids = [str(hit.get("pageid")) for hit in hits if hit.get("pageid")]
        hit_metadata = {
            str(hit.get("pageid")): {
                "search_rank": offset + rank,
                "search_snippet": _plain_search_snippet(hit.get("snippet")),
                "search_title_snippet": _plain_search_snippet(hit.get("titlesnippet")),
            }
            for rank, hit in enumerate(hits, start=1)
            if hit.get("pageid")
        }
        records: list[SourceRecord] = []
        for start in range(0, len(page_ids), 20):
            chunk = page_ids[start : start + 20]
            detail_data = self._api_get(
                {
                    "action": "query",
                    "pageids": "|".join(chunk),
                    "prop": "extracts|info|revisions",
                    "explaintext": 1,
                    "exintro": 0,
                    "inprop": "url",
                    "rvprop": "ids|timestamp",
                }
            )
            pages = detail_data.get("query", {}).get("pages", [])
            if not isinstance(pages, list):
                raise ParseError("MediaWiki 详情响应缺少 query.pages 列表")
            for page in pages:
                page_id = str(page.get("pageid") or "")
                title = normalize_text(page.get("title"))
                content = normalize_text(page.get("extract"))
                url = canonicalize_url(page.get("fullurl") or "")
                revisions = page.get("revisions") or []
                revision = revisions[0] if isinstance(revisions, list) and revisions else {}
                if not page_id:
                    continue
                records.append(
                    SourceRecord(
                        source_name="mediawiki",
                        source_record_id="pageid:" + page_id,
                        title=title,
                        abstract="",
                        content=content,
                        source_url=url,
                        language="zh",
                        raw_locator=url or f"pageid:{page_id}",
                        raw_hash=stable_fingerprint(page),
                        batch_id=f"mediawiki-{category.category_id}",
                        query_text=query,
                        access_basis="public MediaWiki API",
                        rights_statement=self.rights_statement,
                        retrieved_at=utc_now(),
                        raw_metadata={
                            "pageid": page_id,
                            "revision_id": revision.get("revid"),
                            "revision_timestamp": revision.get("timestamp"),
                            **hit_metadata.get(page_id, {}),
                        },
                    )
                )
        continuation = search_data.get("continue", {}).get("sroffset")
        reached_candidate_limit = continuation is not None and int(continuation) >= self.max_candidates_per_category
        exhausted = not hits or continuation is None or reached_candidate_limit
        next_cursor = str(continuation) if continuation is not None else str(offset + len(hits))
        return CollectionPage(
            records=records,
            next_cursor=next_cursor,
            exhausted=exhausted,
            raw_count=len(hits),
        )

    def _api_get(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode(
            {
                **params,
                "format": "json",
                "formatversion": "2",
                "maxlag": self.max_lag,
            }
        )
        url = f"{self.api_url}?{query}"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                if attempt == 0 and self.request_delay:
                    self.sleep(self.request_delay)
                payload = self.transport(
                    url,
                    {"User-Agent": self.user_agent, "Accept": "application/json"},
                    self.timeout,
                    self.max_response_bytes,
                )
                api_error = payload.get("error")
                if isinstance(api_error, dict):
                    code = normalize_text(api_error.get("code"))
                    info = normalize_text(api_error.get("info"))
                    retryable = code in {"maxlag", "ratelimited", "readonly"}
                    raise SourceError(
                        f"MediaWiki API {code or 'error'}: {info or '未知错误'}",
                        retryable=retryable,
                    )
                return payload
            except SourceError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= self.retries:
                    break
                backoff = exc.retry_after
                if backoff is None:
                    backoff = min(5 * (2 ** attempt), 60)
                self.sleep(max(self.request_delay, backoff))
        raise SourceError(f"MediaWiki 请求失败（已重试 {self.retries} 次）: {last_error}", retryable=False)

    @staticmethod
    def _default_transport(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> dict[str, Any]:
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - host validated in constructor
                if not _is_allowed_wikipedia_url(urlsplit(response.geturl())):
                    raise SourceError("MediaWiki 重定向离开了允许的 HTTPS 主机", retryable=False)
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise SourceError("MediaWiki Content-Length 无效", retryable=False) from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise SourceError("MediaWiki 响应超过大小上限", retryable=False)
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise SourceError("MediaWiki 响应超过大小上限", retryable=False)
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise SourceError(
                f"HTTP {exc.code}",
                retryable=retryable,
                retry_after=retry_after,
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise SourceError(str(exc), retryable=True) from exc
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError("MediaWiki 返回的不是有效 JSON", retryable=False) from exc


def _is_allowed_wikipedia_url(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme.lower() == "https"
        and hostname.endswith(".wikipedia.org")
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _plain_search_snippet(value: Any) -> str:
    return normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))))
