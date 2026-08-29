from __future__ import annotations

import re
from datetime import datetime, timezone

from app.models.content import (
    ConsistencyReport,
    ConsistencyStatus,
    ContentProject,
)


_NUMBER_PATTERN = re.compile(r"(?<![\w])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def _numbers(text: str) -> set[str]:
    without_urls = _URL_PATTERN.sub("", text)
    return {match.replace(",", "") for match in _NUMBER_PATTERN.findall(without_urls)}


class ContentConsistencyChecker:
    """Run deterministic claim-reference and numeric consistency checks."""

    def check(self, project: ContentProject) -> ConsistencyReport:
        if project.evidence_pack is None:
            return ConsistencyReport(status=ConsistencyStatus.not_ready)
        channel_data = {}
        if project.blog_output is not None:
            channel_data["blog"] = (
                project.blog_output.evidence_claim_ids,
                "\n".join(
                    [
                        project.blog_output.title,
                        project.blog_output.markdown,
                        project.blog_output.meta_description,
                    ]
                ),
            )
        if project.video_output is not None:
            channel_data["short_video"] = (
                project.video_output.evidence_claim_ids,
                "\n".join(
                    [
                        project.video_output.title,
                        project.video_output.narration,
                        project.video_output.caption,
                    ]
                ),
            )
        if project.newsletter_output is not None:
            channel_data["newsletter"] = (
                project.newsletter_output.evidence_claim_ids,
                "\n".join(
                    [
                        project.newsletter_output.subject,
                        project.newsletter_output.markdown,
                        project.newsletter_output.preview_text,
                    ]
                ),
            )
        if project.social_output is not None:
            channel_data["social"] = (
                project.social_output.evidence_claim_ids,
                "\n".join(
                    post.content for post in project.social_output.posts
                ),
            )
        selected = [channel.value for channel in project.requested_channels]
        if not selected or any(channel not in channel_data for channel in selected):
            return ConsistencyReport(status=ConsistencyStatus.not_ready)

        evidence_text = "\n".join(
            claim.statement for claim in project.evidence_pack.claims
        )
        evidence_numbers = _numbers(evidence_text)
        issues = []
        channel_claim_ids = {}
        claim_sets = []
        for channel in selected:
            claim_ids, text = channel_data[channel]
            channel_claim_ids[channel] = sorted(set(claim_ids))
            claim_sets.append(set(claim_ids))
            unsupported = sorted(_numbers(text) - evidence_numbers)
            if unsupported:
                issues.append(
                    f"{channel} contains numbers absent from approved claims: "
                    + ", ".join(unsupported)
                )

        shared = set.intersection(*claim_sets) if claim_sets else set()
        blog_claim_ids = set(channel_claim_ids.get("blog", []))
        video_claim_ids = set(channel_claim_ids.get("short_video", []))

        return ConsistencyReport(
            status=(
                ConsistencyStatus.warning if issues else ConsistencyStatus.passed
            ),
            checked_at=datetime.now(timezone.utc),
            issues=issues,
            shared_claim_ids=sorted(shared),
            blog_only_claim_ids=sorted(blog_claim_ids - video_claim_ids),
            video_only_claim_ids=sorted(video_claim_ids - blog_claim_ids),
            channel_claim_ids=channel_claim_ids,
        )
