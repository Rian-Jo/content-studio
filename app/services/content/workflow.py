from concurrent.futures import ThreadPoolExecutor, as_completed

from app.models.content import (
    BlogStatus,
    ChannelRunRecord,
    ChannelRunStatus,
    ConsistencyReport,
    ContentChannel,
    ContentFanoutRequest,
    ContentProject,
    ContentProjectCreate,
    GhostStatus,
    LLMUsageRecord,
    VideoStatus,
    utc_now,
)
from app.models.evidence import EvidenceStatus, ResearchRequest
from app.services.content.blog_generator import BlogGenerator
from app.services.content.evidence_builder import EvidenceBuilder
from app.services.content.quality_gate import ContentConsistencyChecker
from app.services.content.research import SourceResearcher
from app.services.content.store import ContentStore
from app.services.content.video_adapter import MoneyPrinterVideoAdapter, VideoGenerator
from app.services.publishers.base import DraftPublisher


class ContentWorkflow:
    def __init__(
        self,
        store: ContentStore | None = None,
        blog_generator: BlogGenerator | None = None,
        researcher: SourceResearcher | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        video_generator: VideoGenerator | None = None,
        consistency_checker: ContentConsistencyChecker | None = None,
    ):
        self.store = store or ContentStore()
        self.blog_generator = blog_generator or BlogGenerator()
        self.researcher = researcher or SourceResearcher()
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        self.video_generator = video_generator or MoneyPrinterVideoAdapter()
        self.consistency_checker = consistency_checker or ContentConsistencyChecker()

    def create_project(self, request: ContentProjectCreate) -> ContentProject:
        return self.store.save(ContentProject(**request.model_dump()))

    def research_evidence(
        self, project_id: str, request: ResearchRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.blog_output is not None or project.video_output is not None:
            raise ValueError(
                "evidence cannot be replaced after a channel output exists; "
                "create a new content project instead"
            )
        project.evidence_status = EvidenceStatus.researching
        project.last_error = None
        self.store.save(project)
        try:
            sources = self.researcher.research(request.sources)
            project.evidence_pack = self.evidence_builder.build(project, sources)
            project.evidence_status = EvidenceStatus.ready_for_review
            project.consistency_report = ConsistencyReport()
        except Exception as exc:
            project.evidence_status = EvidenceStatus.failed
            project.last_error = str(exc)
            self.store.save(project)
            raise
        return self.store.save(project)

    def approve_evidence(
        self, project_id: str, note: str | None = None
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.evidence_pack is None:
            raise ValueError("build an EvidencePack before approving it")
        if project.evidence_status != EvidenceStatus.ready_for_review:
            raise ValueError("only an EvidencePack ready for review can be approved")
        project.evidence_pack.approved_at = utc_now()
        project.evidence_pack.approval_note = note
        project.evidence_status = EvidenceStatus.approved
        project.last_error = None
        return self.store.save(project)

    def generate_blog(self, project_id: str) -> ContentProject:
        project = self.store.get(project_id)
        if (
            project.evidence_status != EvidenceStatus.approved
            or project.evidence_pack is None
        ):
            raise ValueError("approve an EvidencePack before generating a blog draft")
        project.blog_status = BlogStatus.generating
        project.last_error = None
        project.channel_runs[ContentChannel.blog.value] = ChannelRunRecord(
            status=ChannelRunStatus.running,
            started_at=utc_now(),
            llm_usage=self._llm_usage(),
        )
        self.store.save(project)
        try:
            project.blog_output = self.blog_generator.generate(project)
            project.blog_status = BlogStatus.draft_complete
            project.ghost_status = GhostStatus.ready
            blog_run = project.channel_runs[ContentChannel.blog.value]
            blog_run.status = ChannelRunStatus.complete
            blog_run.finished_at = utc_now()
        except Exception as exc:
            project.blog_status = BlogStatus.failed
            project.last_error = str(exc)
            blog_run = project.channel_runs[ContentChannel.blog.value]
            blog_run.status = ChannelRunStatus.failed
            blog_run.finished_at = utc_now()
            blog_run.error = str(exc)[:1000]
            self.store.save(project)
            raise
        project.consistency_report = self.consistency_checker.check(project)
        return self.store.save(project)

    @staticmethod
    def _llm_usage() -> LLMUsageRecord:
        return LLMUsageRecord(
            request_count=1,
            measurement="unavailable",
            note=(
                "The current MoneyPrinterTurbo LLM adapter does not expose token "
                "usage or provider pricing, so cost is not fabricated."
            ),
        )

    def run_fanout(
        self, project_id: str, request: ContentFanoutRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        if (
            project.evidence_status != EvidenceStatus.approved
            or project.evidence_pack is None
        ):
            raise ValueError("approve an EvidencePack before running channel fan-out")

        project.requested_channels = request.channels
        project.last_error = None
        operations = {}
        snapshot = project.model_copy(deep=True)
        started_at = utc_now()

        if ContentChannel.blog in request.channels and project.blog_output is None:
            project.blog_status = BlogStatus.generating
            project.channel_runs[ContentChannel.blog.value] = ChannelRunRecord(
                status=ChannelRunStatus.running,
                started_at=started_at,
                llm_usage=self._llm_usage(),
            )
            operations[ContentChannel.blog] = (
                self.blog_generator.generate,
                (snapshot,),
            )

        video_active = project.video_status in {
            VideoStatus.queued,
            VideoStatus.rendering,
            VideoStatus.complete,
        }
        if ContentChannel.short_video in request.channels and not video_active:
            project.video_status = VideoStatus.planning
            project.channel_runs[ContentChannel.short_video.value] = ChannelRunRecord(
                status=ChannelRunStatus.running,
                started_at=started_at,
                llm_usage=self._llm_usage(),
            )
            operations[ContentChannel.short_video] = (
                self.video_generator.generate,
                (snapshot, request.video_options),
            )

        self.store.save(project)
        if not operations:
            project.consistency_report = self.consistency_checker.check(project)
            return self.store.save(project)

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            future_channels = {
                executor.submit(func, *args): channel
                for channel, (func, args) in operations.items()
            }
            for future in as_completed(future_channels):
                channel = future_channels[future]
                run = project.channel_runs[channel.value]
                try:
                    output = future.result()
                except Exception as exc:
                    run.status = ChannelRunStatus.failed
                    run.finished_at = utc_now()
                    run.error = str(exc)[:1000]
                    project.last_error = f"{channel.value}: {exc}"
                    if channel == ContentChannel.blog:
                        project.blog_status = BlogStatus.failed
                    else:
                        project.video_status = VideoStatus.failed
                else:
                    if channel == ContentChannel.blog:
                        project.blog_output = output
                        project.blog_status = BlogStatus.draft_complete
                        project.ghost_status = GhostStatus.ready
                        run.status = ChannelRunStatus.complete
                        run.finished_at = utc_now()
                    else:
                        project.video_output = output
                        project.video_status = VideoStatus.queued
                        run.status = ChannelRunStatus.queued
                self.store.save(project)

        project.consistency_report = self.consistency_checker.check(project)
        return self.store.save(project)

    def refresh_video(self, project_id: str) -> ContentProject:
        project = self.store.get(project_id)
        if project.video_output is None:
            raise ValueError("start a video channel before refreshing its status")
        status, output, error = self.video_generator.refresh(project.video_output)
        project.video_status = status
        project.video_output = output
        run = project.channel_runs.get(ContentChannel.short_video.value)
        if run is not None:
            if status == VideoStatus.complete:
                run.status = ChannelRunStatus.complete
                run.finished_at = utc_now()
                run.error = None
            elif status == VideoStatus.failed:
                run.status = ChannelRunStatus.failed
                run.finished_at = utc_now()
                run.error = error
            elif status in {VideoStatus.queued, VideoStatus.rendering}:
                run.status = ChannelRunStatus.queued
        if error:
            project.last_error = f"short_video: {error}"
        project.consistency_report = self.consistency_checker.check(project)
        return self.store.save(project)

    def sync_ghost_draft(
        self, project_id: str, publisher: DraftPublisher
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.blog_output is None:
            raise ValueError("generate a blog draft before creating a Ghost draft")
        project.ghost_status = GhostStatus.publishing
        project.last_error = None
        self.store.save(project)
        try:
            project.ghost_publication = publisher.sync_draft(
                project.blog_output, project.ghost_publication
            )
            project.ghost_status = GhostStatus.draft_created
        except Exception as exc:
            project.ghost_status = GhostStatus.failed
            project.last_error = str(exc)
            self.store.save(project)
            raise
        return self.store.save(project)
