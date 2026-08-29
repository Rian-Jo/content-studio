from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from app.models.evidence import (
    SourceInput,
    SourceRecord,
    SourceVerificationStatus,
)


MAX_SOURCE_BYTES = 1_000_000
MAX_EXCERPT_CHARACTERS = 12_000
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/markdown",
    "text/plain",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchError(ValueError):
    pass


class _ReadableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif lowered == "title" and self._ignored_depth == 0:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._in_title:
            self.title_parts.append(normalized)
        self.text_parts.append(normalized)

    @property
    def title(self) -> str | None:
        title = " ".join(self.title_parts).strip()
        return title[:300] or None

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)


def ensure_public_http_url(url: str) -> None:
    try:
        SourceInput(url=url)
    except ValueError as exc:
        raise ResearchError("source URL contains unsupported credentials") from exc
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ResearchError("source URL must use http or https")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ResearchError("local source URLs are not allowed")
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        }
    except OSError as exc:
        raise ResearchError("source hostname could not be resolved") from exc
    if not addresses:
        raise ResearchError("source hostname did not resolve to an address")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ResearchError("source hostname resolved to an invalid address") from exc
        if not parsed_address.is_global:
            raise ResearchError("private or non-public source URLs are not allowed")


class SourceResearcher:
    """Fetch and verify user-supplied public sources without discovering URLs."""

    def __init__(
        self,
        session: requests.Session | None = None,
        url_guard: Callable[[str], None] | None = None,
    ):
        self._session = session or requests.Session()
        self._url_guard = url_guard or ensure_public_http_url

    def research(self, sources: Iterable[SourceInput]) -> list[SourceRecord]:
        records = [self._research_one(source) for source in sources]
        if not any(
            record.verification_status == SourceVerificationStatus.verified
            for record in records
        ):
            raise ResearchError("none of the supplied sources could be verified")
        return records

    def _research_one(self, source: SourceInput) -> SourceRecord:
        checked_at = _utc_now()
        try:
            return self._fetch(source, checked_at)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return SourceRecord(
                requested_url=source.url,
                title=source.title,
                verification_status=SourceVerificationStatus.failed,
                checked_at=checked_at,
                error=message[:500],
            )

    def _fetch(self, source: SourceInput, checked_at: datetime) -> SourceRecord:
        current_url = source.url
        response = None
        for redirect_count in range(4):
            self._url_guard(current_url)
            response = self._session.get(
                current_url,
                allow_redirects=False,
                timeout=(5, 20),
                stream=True,
                headers={"User-Agent": "ContentStudioResearch/1.0"},
            )
            if response.status_code not in REDIRECT_STATUSES:
                break
            location = response.headers.get("location", "").strip()
            response.close()
            if not location:
                raise ResearchError("source redirect did not include a location")
            current_url = urljoin(current_url, location)
            if redirect_count == 3:
                raise ResearchError("source exceeded the redirect limit")
        if response is None:
            raise ResearchError("source request did not return a response")
        try:
            if response.status_code < 200 or response.status_code >= 300:
                raise ResearchError(
                    f"source returned HTTP status {response.status_code}"
                )
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type and media_type not in ALLOWED_CONTENT_TYPES:
                raise ResearchError("source content type is not supported")
            content = self._read_limited_content(response)
            encoding = getattr(response, "encoding", None) or "utf-8"
            decoded = content.decode(encoding, errors="replace")
            title, readable_text = self._extract_readable_text(decoded, media_type)
            excerpt = readable_text[:MAX_EXCERPT_CHARACTERS].strip()
            if len(excerpt) < 80:
                raise ResearchError("source did not contain enough readable text")
            parsed = urlparse(current_url)
            return SourceRecord(
                requested_url=source.url,
                final_url=current_url,
                title=source.title or title,
                publisher=parsed.hostname,
                verification_status=SourceVerificationStatus.verified,
                http_status=response.status_code,
                content_type=media_type or None,
                content_excerpt=excerpt,
                content_sha256=hashlib.sha256(content).hexdigest(),
                checked_at=checked_at,
            )
        finally:
            response.close()

    @staticmethod
    def _read_limited_content(response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ResearchError("source response exceeds the size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _extract_readable_text(content: str, media_type: str) -> tuple[str | None, str]:
        is_html = media_type in {"text/html", "application/xhtml+xml"}
        if is_html or "<html" in content[:500].lower():
            parser = _ReadableHTMLParser()
            parser.feed(content)
            return parser.title, parser.text
        return None, "\n".join(line.strip() for line in content.splitlines() if line.strip())
