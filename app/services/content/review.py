from __future__ import annotations

import hashlib
import json

from app.models.content import (
    ApprovalStatus,
    BlogStatus,
    ConsistencyStatus,
    ContentChannel,
    ContentProject,
    ContentReviewRequest,
    ReviewDecision,
    ReviewRecord,
    VideoStatus,
    utc_now,
)


class ContentReviewService:
    """Create and verify manual approvals bound to exact output snapshots."""

    @staticmethod
    def snapshot_sha256(
        project: ContentProject, channels: list[ContentChannel]
    ) -> str:
        payload = {
            "evidence_pack": (
                project.evidence_pack.model_dump(mode="json")
                if project.evidence_pack
                else None
            ),
            "channels": [channel.value for channel in channels],
            "blog_output": (
                project.blog_output.model_dump(mode="json")
                if ContentChannel.blog in channels and project.blog_output
                else None
            ),
            "video_output": (
                project.video_output.model_dump(mode="json")
                if ContentChannel.short_video in channels and project.video_output
                else None
            ),
            "newsletter_output": (
                project.newsletter_output.model_dump(mode="json")
                if ContentChannel.newsletter in channels
                and project.newsletter_output
                else None
            ),
            "social_output": (
                project.social_output.model_dump(mode="json")
                if ContentChannel.social in channels and project.social_output
                else None
            ),
            "consistency_report": project.consistency_report.model_dump(mode="json"),
        }
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def review(
        self, project: ContentProject, request: ContentReviewRequest
    ) -> ContentProject:
        channels = list(dict.fromkeys(project.requested_channels))
        if not channels:
            raise ValueError("select at least one channel before reviewing outputs")
        self._validate_outputs_exist(project, channels)

        if request.decision == ReviewDecision.approve:
            self._validate_approval_ready(project, channels, request)
            status = ApprovalStatus.approved
        else:
            status = ApprovalStatus.changes_requested

        project.approval_status = status
        project.review_record = ReviewRecord(
            status=status,
            reviewed_at=utc_now(),
            note=(request.note or "").strip() or None,
            reviewed_channels=channels,
            snapshot_sha256=self.snapshot_sha256(project, channels),
            quality_warnings_acknowledged=request.acknowledge_quality_warnings,
        )
        return project

    @staticmethod
    def _validate_outputs_exist(
        project: ContentProject, channels: list[ContentChannel]
    ) -> None:
        if ContentChannel.blog in channels and project.blog_output is None:
            raise ValueError("the selected blog output does not exist")
        if ContentChannel.short_video in channels and project.video_output is None:
            raise ValueError("the selected video output does not exist")
        if ContentChannel.newsletter in channels and project.newsletter_output is None:
            raise ValueError("the selected newsletter output does not exist")
        if ContentChannel.social in channels and project.social_output is None:
            raise ValueError("the selected social output does not exist")

    @staticmethod
    def _validate_approval_ready(
        project: ContentProject,
        channels: list[ContentChannel],
        request: ContentReviewRequest,
    ) -> None:
        if (
            ContentChannel.blog in channels
            and project.blog_status != BlogStatus.draft_complete
        ):
            raise ValueError("the blog channel is not ready for approval")
        if (
            ContentChannel.short_video in channels
            and project.video_status != VideoStatus.complete
        ):
            raise ValueError("the video channel is not ready for approval")
        if (
            ContentChannel.newsletter in channels
            and project.newsletter_status != BlogStatus.draft_complete
        ):
            raise ValueError("the newsletter channel is not ready for approval")
        if (
            ContentChannel.social in channels
            and project.social_status != BlogStatus.draft_complete
        ):
            raise ValueError("the social channel is not ready for approval")
        if len(channels) > 1:
            if project.consistency_report.status == ConsistencyStatus.not_ready:
                raise ValueError("run the cross-channel consistency check first")
            if (
                project.consistency_report.status == ConsistencyStatus.warning
                and not request.acknowledge_quality_warnings
            ):
                raise ValueError(
                    "acknowledge quality warnings before approving the outputs"
                )

    def invalidate(self, project: ContentProject, reason: str) -> None:
        if project.approval_status == ApprovalStatus.waiting_for_review:
            return
        project.approval_status = ApprovalStatus.waiting_for_review
        if project.review_record is not None:
            project.review_record.invalidated_at = utc_now()
            project.review_record.invalidated_reason = reason[:500]

    def is_current_approval(self, project: ContentProject) -> bool:
        record = project.review_record
        if (
            project.approval_status != ApprovalStatus.approved
            or record is None
            or record.status != ApprovalStatus.approved
            or record.invalidated_at is not None
        ):
            return False
        return record.snapshot_sha256 == self.snapshot_sha256(
            project, record.reviewed_channels
        )

    def require_current_channel_approval(
        self, project: ContentProject, channel: ContentChannel
    ) -> None:
        if not self.is_current_approval(project):
            raise ValueError("approve the current output snapshot before publishing")
        if channel not in project.review_record.reviewed_channels:
            raise ValueError(f"the {channel.value} channel was not included in approval")
