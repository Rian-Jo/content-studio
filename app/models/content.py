from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.models.evidence import EvidencePack, EvidenceStatus


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


class VideoStatus(str, Enum):
    not_started = "not_started"
    planning = "planning"
    queued = "queued"
    rendering = "rendering"
    complete = "complete"
    failed = "failed"


class ChannelRunStatus(str, Enum):
    running = "running"
    queued = "queued"
    complete = "complete"
    failed = "failed"


class ConsistencyStatus(str, Enum):
    not_ready = "not_ready"
    passed = "passed"
    warning = "warning"


class ApprovalStatus(str, Enum):
    waiting_for_review = "waiting_for_review"
    approved = "approved"
    changes_requested = "changes_requested"


class ReviewDecision(str, Enum):
    approve = "approve"
    request_changes = "request_changes"


class ReleasePlanStatus(str, Enum):
    ready = "ready"
    stale = "stale"


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
    evidence_claim_ids: list[str] = Field(default_factory=list, max_length=50)


class VideoScene(BaseModel):
    narration: str = Field(min_length=1, max_length=1000)
    visual_direction: str = Field(min_length=1, max_length=1000)


class VideoOutput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    hook: str = Field(min_length=1, max_length=500)
    narration: str = Field(min_length=20, max_length=5000)
    scenes: list[VideoScene] = Field(min_length=1, max_length=20)
    search_terms: list[str] = Field(min_length=1, max_length=10)
    caption: str = Field(min_length=1, max_length=2200)
    hashtags: list[str] = Field(default_factory=list, max_length=20)
    evidence_claim_ids: list[str] = Field(min_length=1, max_length=50)
    task_id: str | None = None
    rendered_files: list[str] = Field(default_factory=list)


class LLMUsageRecord(BaseModel):
    request_count: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    measurement: Literal["reported", "estimated", "unavailable"] = "unavailable"
    note: str | None = Field(default=None, max_length=500)


class ChannelRunRecord(BaseModel):
    status: ChannelRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    llm_usage: LLMUsageRecord = Field(default_factory=LLMUsageRecord)
    error: str | None = Field(default=None, max_length=1000)


class ConsistencyReport(BaseModel):
    status: ConsistencyStatus = ConsistencyStatus.not_ready
    checked_at: datetime | None = None
    issues: list[str] = Field(default_factory=list, max_length=50)
    shared_claim_ids: list[str] = Field(default_factory=list, max_length=50)
    blog_only_claim_ids: list[str] = Field(default_factory=list, max_length=50)
    video_only_claim_ids: list[str] = Field(default_factory=list, max_length=50)


class VideoGenerationOptions(BaseModel):
    video_aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    video_source: Literal["pexels", "pixabay", "coverr"] = "pexels"
    voice_name: str = Field(default="", max_length=300)
    bgm_type: str = Field(default="random", max_length=100)
    bgm_volume: float = Field(default=0.2, ge=0, le=1)
    subtitle_enabled: bool = True


class ContentFanoutRequest(BaseModel):
    channels: list[ContentChannel] = Field(min_length=1, max_length=2)
    video_options: VideoGenerationOptions = Field(
        default_factory=VideoGenerationOptions
    )
    regenerate: bool = False

    @model_validator(mode="after")
    def reject_duplicate_channels(self) -> ContentFanoutRequest:
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("fan-out channels must be unique")
        if (
            ContentChannel.short_video in self.channels
            and not self.video_options.voice_name.strip()
        ):
            raise ValueError("video_options.voice_name is required for short video")
        return self


class ContentReviewRequest(BaseModel):
    decision: ReviewDecision
    note: str | None = Field(default=None, max_length=2000)
    acknowledge_quality_warnings: bool = False

    @model_validator(mode="after")
    def require_changes_note(self) -> ContentReviewRequest:
        if (
            self.decision == ReviewDecision.request_changes
            and not (self.note or "").strip()
        ):
            raise ValueError("a review note is required when requesting changes")
        return self


class ReviewRecord(BaseModel):
    status: ApprovalStatus
    reviewed_at: datetime
    note: str | None = Field(default=None, max_length=2000)
    reviewed_channels: list[ContentChannel] = Field(min_length=1, max_length=2)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_warnings_acknowledged: bool = False
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = Field(default=None, max_length=500)


class ContentReleaseRequest(BaseModel):
    channels: list[ContentChannel] = Field(min_length=1, max_length=2)
    planned_for: datetime | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_release_request(self) -> ContentReleaseRequest:
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("release channels must be unique")
        if self.planned_for is not None and self.planned_for.tzinfo is None:
            raise ValueError("planned_for must include a timezone offset")
        return self


class ReleaseArtifact(BaseModel):
    relative_path: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReleasePlan(BaseModel):
    release_id: str
    status: ReleasePlanStatus = ReleasePlanStatus.ready
    channels: list[ContentChannel] = Field(min_length=1, max_length=2)
    created_at: datetime
    planned_for: datetime | None = None
    note: str | None = Field(default=None, max_length=2000)
    approval_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_path: str = Field(min_length=1, max_length=500)
    archive_path: str = Field(min_length=1, max_length=500)
    artifacts: list[ReleaseArtifact] = Field(min_length=1, max_length=20)
    external_actions_performed: bool = False
    stale_at: datetime | None = None
    stale_reason: str | None = Field(default=None, max_length=500)


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
    evidence_pack: EvidencePack | None = None
    evidence_status: EvidenceStatus = EvidenceStatus.not_started
    blog_output: BlogOutput | None = None
    blog_status: BlogStatus = BlogStatus.not_started
    video_output: VideoOutput | None = None
    video_status: VideoStatus = VideoStatus.not_started
    channel_runs: dict[str, ChannelRunRecord] = Field(default_factory=dict)
    consistency_report: ConsistencyReport = Field(default_factory=ConsistencyReport)
    ghost_status: GhostStatus = GhostStatus.not_configured
    ghost_publication: GhostPublication | None = None
    approval_status: ApprovalStatus = ApprovalStatus.waiting_for_review
    review_record: ReviewRecord | None = None
    release_plans: list[ReleasePlan] = Field(default_factory=list, max_length=50)
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
