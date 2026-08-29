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
        if (
            project.evidence_pack is None
            or project.blog_output is None
            or project.video_output is None
        ):
            return ConsistencyReport(status=ConsistencyStatus.not_ready)

        blog_claim_ids = set(project.blog_output.evidence_claim_ids)
        video_claim_ids = set(project.video_output.evidence_claim_ids)
        evidence_text = "\n".join(
            claim.statement for claim in project.evidence_pack.claims
        )
        blog_text = "\n".join(
            [
                project.blog_output.title,
                project.blog_output.markdown,
                project.blog_output.meta_description,
            ]
        )
        video_text = "\n".join(
            [
                project.video_output.title,
                project.video_output.narration,
                project.video_output.caption,
            ]
        )
        evidence_numbers = _numbers(evidence_text)
        unsupported_blog = sorted(_numbers(blog_text) - evidence_numbers)
        unsupported_video = sorted(_numbers(video_text) - evidence_numbers)

        issues = []
        if unsupported_blog:
            issues.append(
                "blog contains numbers absent from approved claims: "
                + ", ".join(unsupported_blog)
            )
        if unsupported_video:
            issues.append(
                "video contains numbers absent from approved claims: "
                + ", ".join(unsupported_video)
            )

        return ConsistencyReport(
            status=(
                ConsistencyStatus.warning if issues else ConsistencyStatus.passed
            ),
            checked_at=datetime.now(timezone.utc),
            issues=issues,
            shared_claim_ids=sorted(blog_claim_ids & video_claim_ids),
            blog_only_claim_ids=sorted(blog_claim_ids - video_claim_ids),
            video_only_claim_ids=sorted(video_claim_ids - blog_claim_ids),
        )
