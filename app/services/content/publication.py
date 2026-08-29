from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urljoin
from uuid import uuid4

import requests

from app.models.content import (
    ContentProject,
    PublicationObservation,
    PublicationObservationRequest,
    PublicationReachability,
    PublicationReceipt,
    PublicationReceiptRequest,
    ReleasePlanStatus,
    utc_now,
)
from app.services.content.research import REDIRECT_STATUSES, ensure_public_http_url
from app.services.content.review import ContentReviewService


class PublicationUrlVerifier:
    """Check a public URL without following an unvalidated redirect."""

    def __init__(
        self,
        session: requests.Session | None = None,
        url_guard: Callable[[str], None] | None = None,
    ):
        self._session = session or requests.Session()
        self._url_guard = url_guard or ensure_public_http_url

    def verify(
        self, url: str, metrics: PublicationObservationRequest
    ) -> PublicationObservation:
        current_url = url
        response = None
        started_at = time.monotonic()
        try:
            for redirect_count in range(4):
                self._guard(current_url)
                response = self._session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=(5, 20),
                    stream=True,
                    headers={"User-Agent": "ContentStudioPublicationVerifier/1.0"},
                )
                if response.status_code not in REDIRECT_STATUSES:
                    break
                location = response.headers.get("location", "").strip()
                response.close()
                response = None
                if not location:
                    return self._observation(
                        metrics,
                        started_at,
                        current_url,
                        error="publication redirect did not include a location",
                    )
                current_url = urljoin(current_url, location)
                if redirect_count == 3:
                    return self._observation(
                        metrics,
                        started_at,
                        current_url,
                        error="publication URL exceeded the redirect limit",
                    )
        except requests.RequestException as exc:
            return self._observation(
                metrics,
                started_at,
                current_url,
                error=(str(exc).strip() or type(exc).__name__)[:500],
            )

        if response is None:
            return self._observation(
                metrics,
                started_at,
                current_url,
                error="publication URL did not return a response",
            )
        try:
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower() or None
            reachable = 200 <= response.status_code < 300
            return self._observation(
                metrics,
                started_at,
                current_url,
                http_status=response.status_code,
                content_type=media_type,
                error=(
                    None
                    if reachable
                    else f"publication returned HTTP status {response.status_code}"
                ),
            )
        finally:
            response.close()

    def _guard(self, url: str) -> None:
        try:
            self._url_guard(url)
        except ValueError as exc:
            message = str(exc).replace("source", "publication")
            raise ValueError(message) from exc

    @staticmethod
    def _observation(
        metrics: PublicationObservationRequest,
        started_at: float,
        final_url: str,
        http_status: int | None = None,
        content_type: str | None = None,
        error: str | None = None,
    ) -> PublicationObservation:
        elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
        return PublicationObservation(
            observed_at=utc_now(),
            reachability=(
                PublicationReachability.reachable
                if error is None and http_status is not None
                else PublicationReachability.unreachable
            ),
            final_url=final_url,
            http_status=http_status,
            content_type=content_type,
            response_time_ms=elapsed_ms,
            error=error,
            **metrics.model_dump(),
        )


class ContentPublicationService:
    """Record user-reported publications; never publish content itself."""

    def __init__(
        self,
        verifier: PublicationUrlVerifier | None = None,
        review_service: ContentReviewService | None = None,
    ):
        self.verifier = verifier or PublicationUrlVerifier()
        self.review_service = review_service or ContentReviewService()

    def record(
        self, project: ContentProject, request: PublicationReceiptRequest
    ) -> ContentProject:
        plan = next(
            (
                candidate
                for candidate in project.release_plans
                if candidate.release_id == request.release_id
            ),
            None,
        )
        if plan is None:
            raise ValueError("release plan was not found")
        if plan.status != ReleasePlanStatus.ready:
            raise ValueError("a stale release plan cannot receive a publication URL")
        if request.channel not in plan.channels:
            raise ValueError("publication channel is not included in the release plan")
        if not self.review_service.is_current_approval(project):
            raise ValueError("the release approval is no longer current")
        if project.review_record.snapshot_sha256 != plan.approval_snapshot_sha256:
            raise ValueError("release plan does not match the current approval snapshot")
        if len(project.publication_receipts) >= 100:
            raise ValueError("a content project can store at most 100 publications")
        if any(
            receipt.release_id == request.release_id
            and receipt.channel == request.channel
            and receipt.public_url == request.public_url
            for receipt in project.publication_receipts
        ):
            raise ValueError("this publication URL is already recorded for the release")

        observation = self.verifier.verify(
            request.public_url, PublicationObservationRequest()
        )
        project.publication_receipts.append(
            PublicationReceipt(
                receipt_id=str(uuid4()),
                release_id=request.release_id,
                channel=request.channel,
                platform=request.platform,
                public_url=request.public_url,
                published_at=request.published_at,
                recorded_at=utc_now(),
                approval_snapshot_sha256=plan.approval_snapshot_sha256,
                note=(request.note or "").strip() or None,
                external_action_performed_by_studio=False,
                observations=[observation],
            )
        )
        return project

    def observe(
        self,
        project: ContentProject,
        receipt_id: str,
        request: PublicationObservationRequest,
    ) -> ContentProject:
        receipt = next(
            (
                candidate
                for candidate in project.publication_receipts
                if candidate.receipt_id == receipt_id
            ),
            None,
        )
        if receipt is None:
            raise ValueError("publication receipt was not found")
        if len(receipt.observations) >= 100:
            raise ValueError("a publication can store at most 100 observations")
        receipt.observations.append(self.verifier.verify(receipt.public_url, request))
        return project
