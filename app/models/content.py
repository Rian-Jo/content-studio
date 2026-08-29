from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContentChannel(str, Enum):
    blog = "blog"
    short_video = "short_video"


class BlogStatus(str, Enum):
    not_started = "not_started"
    generating = "generating"
    draft_complete = "draft_complete"
    failed = "failed"


class GhostStatus(str, Enum):
    not_configured = "not_configured"
    ready = "ready"
    publishing = "publishing"
    draft_created = "draft_created"
    failed = "failed"


class ApprovalStatus(str, Enum):
    waiting_for_review = "waiting_for_review"
    approved = "approved"
    changes_requested = "changes_requested"


class BlogOutput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=191, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    excerpt: str = Field(min_length=1, max_length=500)
    markdown: str = Field(min_length=100)
    html: str = Field(min_length=100)
    seo_title: str = Field(min_length=1, max_length=70)
    meta_description: str = Field(min_length=1, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=10)
    feature_image: str | None = None


class GhostPublication(BaseModel):
    post_id: str
    updated_at: str
    status: Literal["draft"] = "draft"
    url: str | None = None


class ContentProjectCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    audience: str = Field(default="general", min_length=1, max_length=300)
    objective: str = Field(default="inform", min_length=1, max_length=300)
    language: str = Field(default="auto", min_length=1, max_length=64)
    requested_channels: list[ContentChannel] = Field(
        default_factory=lambda: [ContentChannel.blog], min_length=1
    )


class ContentProject(ContentProjectCreate):
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    blog_output: BlogOutput | None = None
    blog_status: BlogStatus = BlogStatus.not_started
    ghost_status: GhostStatus = GhostStatus.not_configured
    ghost_publication: GhostPublication | None = None
    approval_status: ApprovalStatus = ApprovalStatus.waiting_for_review
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ContentProjectResponse(BaseModel):
    status: int = 200
    message: str | None = "success"
    data: ContentProject


class ContentProjectListData(BaseModel):
    projects: list[ContentProject]


class ContentProjectListResponse(BaseModel):
    status: int = 200
    message: str | None = "success"
    data: ContentProjectListData
