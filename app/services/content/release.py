from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

from app.models.content import (
    ContentChannel,
    ContentProject,
    ContentReleaseRequest,
    ReleaseArtifact,
    ReleasePlan,
    ReleasePlanStatus,
    utc_now,
)
from app.services.content.review import ContentReviewService
from app.utils import utils


class ContentReleaseService:
    """Build local release bundles without calling an external publisher."""

    def __init__(
        self,
        release_root: str | Path | None = None,
        review_service: ContentReviewService | None = None,
    ):
        if release_root is None:
            content_root = Path(utils.storage_dir("content", create=True))
            self.release_root = content_root / "releases"
        else:
            self.release_root = Path(release_root)
        self.storage_root = self.release_root.parent
        self.review_service = review_service or ContentReviewService()

    def create(
        self, project: ContentProject, request: ContentReleaseRequest
    ) -> ContentProject:
        if not self.review_service.is_current_approval(project):
            raise ValueError("approve the current output snapshot before release export")
        record = project.review_record
        if record is None:
            raise ValueError("an approval record is required for release export")
        for channel in request.channels:
            self.review_service.require_current_channel_approval(project, channel)
        if len(project.release_plans) >= 50:
            raise ValueError("a content project can store at most 50 release plans")

        release_id = str(uuid4())
        project_root = self._safe_child(self.release_root, project.project_id)
        bundle_root = self._safe_child(project_root, release_id)
        temporary_root = self._safe_child(project_root, f".{release_id}.tmp")
        archive_path = self._safe_child(project_root, f"{release_id}.zip")
        temporary_archive = self._safe_child(project_root, f".{release_id}.zip.tmp")
        project_root.mkdir(parents=True, exist_ok=True)
        temporary_root.mkdir()

        try:
            artifact_specs = self._write_bundle_files(
                temporary_root, project, request, release_id
            )
            manifest_payload = self._manifest_payload(
                temporary_root, project, request, release_id, artifact_specs
            )
            manifest_path = temporary_root / "manifest.json"
            self._write_json(manifest_path, manifest_payload)
            artifact_specs.append(("manifest.json", "application/json"))
            temporary_root.rename(bundle_root)

            with zipfile.ZipFile(
                temporary_archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for file_path in sorted(bundle_root.rglob("*")):
                    if file_path.is_file():
                        archive.write(file_path, file_path.relative_to(bundle_root))
            temporary_archive.replace(archive_path)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            shutil.rmtree(bundle_root, ignore_errors=True)
            temporary_archive.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
            raise

        artifacts = [
            self._artifact(bundle_root / relative_path, media_type)
            for relative_path, media_type in artifact_specs
        ]
        artifacts.append(self._artifact(archive_path, "application/zip"))
        manifest_sha256 = self._sha256(bundle_root / "manifest.json")
        project.release_plans.append(
            ReleasePlan(
                release_id=release_id,
                channels=request.channels,
                created_at=utc_now(),
                planned_for=request.planned_for,
                note=(request.note or "").strip() or None,
                approval_snapshot_sha256=record.snapshot_sha256,
                manifest_sha256=manifest_sha256,
                bundle_path=self._relative(bundle_root),
                archive_path=self._relative(archive_path),
                artifacts=artifacts,
                external_actions_performed=False,
            )
        )
        return project

    def invalidate(self, project: ContentProject, reason: str) -> None:
        for plan in project.release_plans:
            if plan.status == ReleasePlanStatus.ready:
                plan.status = ReleasePlanStatus.stale
                plan.stale_at = utc_now()
                plan.stale_reason = reason[:500]

    def _write_bundle_files(
        self,
        root: Path,
        project: ContentProject,
        request: ContentReleaseRequest,
        release_id: str,
    ) -> list[tuple[str, str]]:
        specs: list[tuple[str, str]] = []

        def write_json(relative_path: str, payload: object) -> None:
            self._write_json(root / relative_path, payload)
            specs.append((relative_path, "application/json"))

        write_json(
            "evidence.json",
            project.evidence_pack.model_dump(mode="json")
            if project.evidence_pack
            else None,
        )
        write_json("review.json", project.review_record.model_dump(mode="json"))
        write_json(
            "consistency.json", project.consistency_report.model_dump(mode="json")
        )

        if ContentChannel.blog in request.channels and project.blog_output is not None:
            blog = project.blog_output
            self._write_text(root / "blog" / "article.md", blog.markdown)
            specs.append(("blog/article.md", "text/markdown"))
            self._write_text(root / "blog" / "article.html", blog.html)
            specs.append(("blog/article.html", "text/html"))
            write_json("blog/metadata.json", blog.model_dump(mode="json"))

        if (
            ContentChannel.short_video in request.channels
            and project.video_output is not None
        ):
            video_payload = project.video_output.model_dump(mode="json")
            video_payload["rendered_files"] = [
                Path(path).name for path in project.video_output.rendered_files
            ]
            video_payload["media_files_copied"] = False
            write_json("short-video/plan.json", video_payload)

        if (
            ContentChannel.newsletter in request.channels
            and project.newsletter_output is not None
        ):
            newsletter = project.newsletter_output
            self._write_text(root / "newsletter" / "newsletter.md", newsletter.markdown)
            specs.append(("newsletter/newsletter.md", "text/markdown"))
            self._write_text(root / "newsletter" / "newsletter.html", newsletter.html)
            specs.append(("newsletter/newsletter.html", "text/html"))
            write_json("newsletter/metadata.json", newsletter.model_dump(mode="json"))

        if ContentChannel.social in request.channels and project.social_output is not None:
            write_json(
                "social/posts.json", project.social_output.model_dump(mode="json")
            )

        write_json(
            "release.json",
            {
                "release_id": release_id,
                "project_id": project.project_id,
                "topic": project.topic,
                "channels": [channel.value for channel in request.channels],
                "planned_for": (
                    request.planned_for.isoformat() if request.planned_for else None
                ),
                "note": (request.note or "").strip() or None,
                "external_actions_performed": False,
            },
        )
        return specs

    def _manifest_payload(
        self,
        root: Path,
        project: ContentProject,
        request: ContentReleaseRequest,
        release_id: str,
        artifact_specs: list[tuple[str, str]],
    ) -> dict:
        record = project.review_record
        return {
            "schema_version": 1,
            "release_id": release_id,
            "project_id": project.project_id,
            "topic": project.topic,
            "channels": [channel.value for channel in request.channels],
            "approval_snapshot_sha256": record.snapshot_sha256,
            "reviewed_at": record.reviewed_at.isoformat(),
            "planned_for": (
                request.planned_for.isoformat() if request.planned_for else None
            ),
            "external_actions_performed": False,
            "artifacts": [
                {
                    "path": relative_path,
                    "media_type": media_type,
                    "size_bytes": self._safe_child(root, relative_path).stat().st_size,
                    "sha256": self._sha256(
                        self._safe_child(root, relative_path)
                    ),
                }
                for relative_path, media_type in artifact_specs
            ],
        }

    def _artifact(self, path: Path, media_type: str) -> ReleaseArtifact:
        return ReleaseArtifact(
            relative_path=self._relative(path),
            media_type=media_type,
            size_bytes=path.stat().st_size,
            sha256=self._sha256(path),
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.storage_root.resolve()).as_posix()

    @staticmethod
    def _safe_child(root: Path, child: str) -> Path:
        candidate = (root / child).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise ValueError("release path escapes the configured storage root")
        return candidate

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        ContentReleaseService._write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
