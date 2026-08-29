from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

import requests

from app.models.content import utc_now
from app.models.evidence import (
    SearchResultRecord,
    SourceDiscoveryRecord,
    SourceDiscoveryRequest,
    SourceInput,
)
from app.services.content.research import ensure_public_http_url


class SearchProvider(Protocol):
    def search(self, request: SourceDiscoveryRequest, fallback_query: str) -> SourceDiscoveryRecord: ...


class BraveSearchProvider:
    API_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        result_guard: Callable[[str], SourceInput] | None = None,
    ):
        self._api_key = api_key
        self._session = session or requests.Session()
        self._result_guard = result_guard or self._guard_public_result

    @staticmethod
    def _guard_public_result(url: str) -> SourceInput:
        ensure_public_http_url(url)
        return SourceInput(url=url)

    def search(
        self, request: SourceDiscoveryRequest, fallback_query: str
    ) -> SourceDiscoveryRecord:
        api_key = (self._api_key or os.getenv("BRAVE_SEARCH_API_KEY", "")).strip()
        if not api_key:
            raise ValueError(
                "automatic source discovery is not configured; set "
                "BRAVE_SEARCH_API_KEY on the server"
            )
        query = request.query or " ".join(fallback_query.split())
        if not query:
            raise ValueError("source discovery query cannot be empty")
        response = self._session.get(
            self.API_URL,
            params={
                "q": query,
                "count": request.count,
                "country": request.country,
                "search_lang": request.search_lang,
                "safesearch": "strict",
            },
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
        raw_results = payload.get("web", {}).get("results", [])
        candidates: list[SearchResultRecord] = []
        seen_urls: set[str] = set()
        for raw in raw_results:
            if len(candidates) >= request.count:
                break
            url = str(raw.get("url") or "").strip()
            title = " ".join(str(raw.get("title") or "").split())
            if not url or not title:
                continue
            try:
                normalized = self._result_guard(url)
            except ValueError:
                continue
            if normalized.url in seen_urls:
                continue
            seen_urls.add(normalized.url)
            description = " ".join(str(raw.get("description") or "").split())
            candidates.append(
                SearchResultRecord(
                    rank=len(candidates) + 1,
                    title=title,
                    url=normalized.url,
                    description=description or None,
                )
            )
        if not candidates:
            raise ValueError("Brave Search returned no safe web results")
        return SourceDiscoveryRecord(
            query=query,
            searched_at=utc_now(),
            candidates=candidates,
        )
