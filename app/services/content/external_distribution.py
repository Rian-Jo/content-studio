from __future__ import annotations

import hashlib
import os
from datetime import timezone
from pathlib import Path
from typing import Protocol

import requests

from app.models.content import (
    ContentChannel,
    ContentProject,
    ExternalJobStatus,
    ExternalPublicationJob,
    ExternalVideoPublishRequest,
    ProviderAnalyticsSnapshot,
    ReleasePlanStatus,
    utc_now,
)
from app.services.content.review import ContentReviewService
from app.utils import utils


class ExternalVideoGateway(Protocol):
    def submit_video(
        self,
        video_path: Path,
        title: str,
        caption: str,
        platforms: list[str],
        scheduled_for,
        idempotency_key: str,
    ) -> dict: ...

    def status(self, provider_request_id: str | None, provider_job_id: str | None) -> dict: ...

    def analytics(self, provider_request_id: str) -> dict: ...


class UploadPostGateway:
    API_BASE = "https://api.upload-post.com"

    def __init__(self, api_key: str | None = None, username: str | None = None, session=None):
        self.api_key = api_key if api_key is not None else os.getenv("UPLOAD_POST_API_KEY", "")
        self.username = username if username is not None else os.getenv("UPLOAD_POST_USERNAME", "")
        self._session = session or requests.Session()

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        if not self.api_key or not self.username:
            raise ValueError(
                "Upload-Post is not configured; set UPLOAD_POST_API_KEY and "
                "UPLOAD_POST_USERNAME on the server"
            )
        headers = {"Authorization": f"Apikey {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def submit_video(
        self,
        video_path: Path,
        title: str,
        caption: str,
        platforms: list[str],
        scheduled_for,
        idempotency_key: str,
    ) -> dict:
        data = [
            ("user", self.username),
            ("title", title[:2200]),
            ("caption", caption[:5000]),
            ("async_upload", "true"),
            ("external_id", idempotency_key),
        ]
        data.extend(("platform[]", platform) for platform in platforms)
        if scheduled_for is not None:
            data.append(
                (
                    "scheduled_date",
                    scheduled_for.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
            )
        with video_path.open("rb") as video_file:
            response = self._session.post(
                f"{self.API_BASE}/api/upload",
                headers=self._headers(idempotency_key),
                data=data,
                files={"video": video_file},
                timeout=300,
            )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Upload-Post returned an invalid submission response")
        return result

    def status(self, provider_request_id: str | None, provider_job_id: str | None) -> dict:
        params = (
            {"request_id": provider_request_id}
            if provider_request_id
            else {"job_id": provider_job_id}
        )
        response = self._session.get(
            f"{self.API_BASE}/api/uploadposts/status",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def analytics(self, provider_request_id: str) -> dict:
        response = self._session.get(
            f"{self.API_BASE}/api/uploadposts/post-analytics/{provider_request_id}",
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


class ContentExternalDistributionService:
    def __init__(
        self,
        gateway: ExternalVideoGateway | None = None,
        review_service: ContentReviewService | None = None,
        media_root: str | Path | None = None,
    ):
        self.gateway = gateway or UploadPostGateway()
        self.review_service = review_service or ContentReviewService()
        self.media_root = Path(media_root or utils.storage_dir()).resolve()

    def publish_video(
        self, project: ContentProject, request: ExternalVideoPublishRequest
    ) -> ContentProject:
        self.review_service.require_current_channel_approval(
            project, ContentChannel.short_video
        )
        release = next(
            (plan for plan in project.release_plans if plan.release_id == request.release_id),
            None,
        )
        if release is None or release.status != ReleasePlanStatus.ready:
            raise ValueError("a matching ready release plan is required")
        if ContentChannel.short_video not in release.channels:
            raise ValueError("the release plan does not include short_video")
        if project.video_output is None:
            raise ValueError("the approved video output does not exist")
        try:
            rendered_path = project.video_output.rendered_files[request.rendered_file_index]
        except IndexError as exc:
            raise ValueError("the selected rendered video does not exist") from exc
        video_path = Path(rendered_path).resolve()
        if not video_path.is_relative_to(self.media_root) or not video_path.is_file():
            raise ValueError("the rendered video must be an existing file under storage")

        idempotency_key = hashlib.sha256(
            (
                f"{project.project_id}:{release.release_id}:short_video:"
                f"{','.join(request.platforms)}:{request.scheduled_for}"
            ).encode("utf-8")
        ).hexdigest()
        if any(job.idempotency_key == idempotency_key for job in project.external_publication_jobs):
            raise ValueError("this external publication request was already submitted")

        result = self.gateway.submit_video(
            video_path,
            project.video_output.title,
            project.video_output.caption,
            request.platforms,
            request.scheduled_for,
            idempotency_key,
        )
        request_id = result.get("request_id")
        provider_job_id = result.get("job_id")
        if not request_id and not provider_job_id:
            raise ValueError("external publisher returned no request or job identifier")
        now = utc_now()
        project.external_publication_jobs.append(
            ExternalPublicationJob(
                release_id=release.release_id,
                platforms=request.platforms,
                status=(
                    ExternalJobStatus.scheduled
                    if request.scheduled_for
                    else ExternalJobStatus.submitted
                ),
                provider_request_id=request_id,
                provider_job_id=provider_job_id,
                scheduled_for=request.scheduled_for,
                submitted_at=now,
                updated_at=now,
                idempotency_key=idempotency_key,
            )
        )
        return project

    def refresh(self, project: ContentProject, job_id: str) -> ContentProject:
        job = self._job(project, job_id)
        result = self.gateway.status(job.provider_request_id, job.provider_job_id)
        provider_status = str(result.get("status", "")).lower()
        job.status = {
            "pending": ExternalJobStatus.scheduled if job.scheduled_for else ExternalJobStatus.submitted,
            "queued": ExternalJobStatus.processing,
            "processing": ExternalJobStatus.processing,
            "in_progress": ExternalJobStatus.processing,
            "completed": ExternalJobStatus.completed,
            "failed": ExternalJobStatus.failed,
        }.get(provider_status, job.status)
        job.error = str(result.get("error") or result.get("message") or "")[:500] or None
        job.updated_at = utc_now()
        return project

    def refresh_analytics(self, project: ContentProject, job_id: str) -> ContentProject:
        job = self._job(project, job_id)
        if not job.provider_request_id:
            raise ValueError("provider analytics requires a completed request identifier")
        result = self.gateway.analytics(job.provider_request_id)
        metrics = self._numeric_metrics(result)
        job.analytics.append(
            ProviderAnalyticsSnapshot(captured_at=utc_now(), metrics=metrics)
        )
        job.updated_at = utc_now()
        return project

    @staticmethod
    def _job(project: ContentProject, job_id: str) -> ExternalPublicationJob:
        job = next((item for item in project.external_publication_jobs if item.job_id == job_id), None)
        if job is None:
            raise ValueError("external publication job not found")
        return job

    @staticmethod
    def _numeric_metrics(value, prefix: str = "") -> dict[str, int | float]:
        allowed = {"views", "likes", "comments", "shares", "clicks", "impressions", "reach", "saves"}
        metrics = {}
        if isinstance(value, dict):
            for key, child in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                if str(key).lower() in allowed and isinstance(child, (int, float)) and not isinstance(child, bool):
                    metrics[name] = child
                elif isinstance(child, (dict, list)):
                    metrics.update(ContentExternalDistributionService._numeric_metrics(child, name))
        elif isinstance(value, list):
            for index, child in enumerate(value[:20]):
                metrics.update(ContentExternalDistributionService._numeric_metrics(child, f"{prefix}.{index}"))
        return dict(list(metrics.items())[:100])
