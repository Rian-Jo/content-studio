from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.models import const
from app.models.content import (
    ContentProject,
    VideoGenerationOptions,
    VideoOutput,
    VideoStatus,
)
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as video_task
from app.services.content.video_generator import VideoPlanGenerator
from app.utils import utils


class VideoGenerator(Protocol):
    def generate(
        self, project: ContentProject, options: VideoGenerationOptions
    ) -> VideoOutput: ...

    def refresh(self, output: VideoOutput) -> tuple[VideoStatus, VideoOutput, str | None]: ...


def _default_scheduler(func: Callable, **kwargs) -> None:
    # Imported lazily to avoid a controller/content-service import cycle while
    # still sharing MoneyPrinterTurbo's configured queue and concurrency limits.
    from app.controllers.v1.video import task_manager

    task_manager.add_task(func, **kwargs)


class MoneyPrinterVideoAdapter:
    """Submit an evidence-derived plan to MPT without enabling cross-posting."""

    def __init__(
        self,
        plan_generator: VideoPlanGenerator | None = None,
        scheduler: Callable[..., None] | None = None,
    ):
        self.plan_generator = plan_generator or VideoPlanGenerator()
        self._scheduler = scheduler or _default_scheduler

    def generate(
        self, project: ContentProject, options: VideoGenerationOptions
    ) -> VideoOutput:
        output = self.plan_generator.generate(project, options.video_profile)
        task_id = utils.get_uuid()
        params = VideoParams(
            video_subject=output.title,
            video_script=output.narration,
            video_terms=output.search_terms,
            video_aspect=options.video_aspect,
            video_source=options.video_source,
            video_language="" if project.language == "auto" else project.language,
            voice_name=options.voice_name,
            bgm_type=options.bgm_type,
            bgm_volume=options.bgm_volume,
            subtitle_enabled=options.subtitle_enabled,
            video_count=1,
            paragraph_number=6 if options.video_profile == "long" else 1,
        )
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=0,
            content_project_id=project.project_id,
        )
        try:
            self._scheduler(
                video_task.start,
                task_id=task_id,
                params=params,
                stop_at="video",
                allow_cross_post=False,
            )
        except Exception:
            sm.state.delete_task(task_id)
            raise
        return output.model_copy(update={"task_id": task_id})

    def refresh(
        self, output: VideoOutput
    ) -> tuple[VideoStatus, VideoOutput, str | None]:
        if not output.task_id:
            return VideoStatus.failed, output, "video output has no task ID"
        task = sm.state.get_task(output.task_id)
        if task is None:
            return VideoStatus.failed, output, "video task state was not found"
        state = task.get("state")
        if state == const.TASK_STATE_FAILED:
            return VideoStatus.failed, output, str(
                task.get("error") or "video generation failed"
            )
        if state == const.TASK_STATE_COMPLETE:
            rendered_files = [str(path) for path in task.get("videos") or []]
            return (
                VideoStatus.complete,
                output.model_copy(update={"rendered_files": rendered_files}),
                None,
            )
        progress = int(task.get("progress") or 0)
        status = VideoStatus.rendering if progress > 0 else VideoStatus.queued
        return status, output, None
