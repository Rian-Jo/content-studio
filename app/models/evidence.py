from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from urllib.parse import parse_qsl, urldefrag, urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class SourceVerificationStatus(str, Enum):
    verified = "verified"
    failed = "failed"


class EvidenceStatus(str, Enum):
    not_started = "not_started"
    researching = "researching"
    ready_for_review = "ready_for_review"
    approved = "approved"
    changes_requested = "changes_requested"
    failed = "failed"


class SourceInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        normalized = urldefrag(value.strip()).url
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("source URL must not contain credentials")
        sensitive_query_terms = {
            "access_token",
            "api-key",
            "api_key",
            "apikey",
            "auth",
            "auth_token",
            "authorization",
            "key",
            "password",
            "secret",
            "sig",
            "signature",
            "token",
            "x-api-key",
        }
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
        if query_keys & sensitive_query_terms:
            raise ValueError("source URL must not contain credentials in its query")
        return normalized


class ResearchRequest(BaseModel):
    sources: list[SourceInput] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def reject_duplicate_urls(self) -> ResearchRequest:
        urls = [source.url for source in self.sources]
        if len(urls) != len(set(urls)):
            raise ValueError("source URLs must be unique")
        return self


class SourceDiscoveryRequest(BaseModel):
    query: str | None = Field(default=None, max_length=400)
    count: int = Field(default=5, ge=1, le=10)
    country: str = Field(default="US", min_length=2, max_length=2)
    search_lang: str = Field(default="en", min_length=2, max_length=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        normalized = " ".join((value or "").split())
        return normalized or None

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("country must use a two-letter code")
        return value.upper()


class SearchResultRecord(BaseModel):
    rank: int = Field(ge=1, le=20)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=2000)


class SourceDiscoveryRecord(BaseModel):
    provider: Literal["brave"] = "brave"
    query: str = Field(min_length=1, max_length=400)
    searched_at: datetime
    candidates: list[SearchResultRecord] = Field(min_length=1, max_length=10)


class SourceRecord(BaseModel):
    source_id: str = Field(default_factory=lambda: str(uuid4()))
    requested_url: str = Field(max_length=2048)
    final_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    publisher: str | None = Field(default=None, max_length=255)
    verification_status: SourceVerificationStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = Field(default=None, max_length=255)
    content_excerpt: str | None = Field(default=None, max_length=12000)
    content_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    checked_at: datetime
    error: str | None = Field(default=None, max_length=500)


class ClaimRecord(BaseModel):
    claim_id: str = Field(default_factory=lambda: str(uuid4()))
    statement: str = Field(min_length=1, max_length=1500)
    source_ids: list[str] = Field(min_length=1, max_length=10)
    confidence: Literal["high", "medium", "low"] = "medium"


class EvidencePack(BaseModel):
    sources: list[SourceRecord] = Field(min_length=1, max_length=10)
    claims: list[ClaimRecord] = Field(min_length=1, max_length=50)
    key_messages: list[str] = Field(default_factory=list, max_length=20)
    counterpoints: list[str] = Field(default_factory=list, max_length=20)
    seo_keywords: list[str] = Field(default_factory=list, max_length=20)
    checked_at: datetime
    approved_at: datetime | None = None
    approval_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_claim_sources(self) -> EvidencePack:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("evidence source IDs must be unique")
        verified_ids = {
            source.source_id
            for source in self.sources
            if source.verification_status == SourceVerificationStatus.verified
        }
        for claim in self.claims:
            unknown = set(claim.source_ids) - verified_ids
            if unknown:
                raise ValueError(
                    "every claim must reference only verified evidence sources"
                )
        return self


class EvidenceApprovalRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
