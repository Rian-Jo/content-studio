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
    ContentReleaseRequest,
    ContentReviewRequest,
    ExternalVideoPublishRequest,
    GhostStatus,
    GhostPublicationRequest,
    LLMUsageRecord,
    PublicationObservationRequest,
    PublicationReceiptRequest,
    ReleasePlanStatus,
    VideoStatus,
    utc_now,
)
from app.models.evidence import (
    EvidenceStatus,
    ResearchRequest,
    SourceDiscoveryRequest,
    SourceInput,
)
from app.services.content.blog_generator import BlogGenerator
from app.services.content.evidence_builder import EvidenceBuilder
from app.services.content.external_distribution import ContentExternalDistributionService
from app.services.content.distribution_generator import (
    NewsletterGenerator,
    SocialGenerator,
)
from app.services.content.quality_gate import ContentConsistencyChecker
from app.services.content.research import SourceResearcher
from app.services.content.publication import ContentPublicationService
from app.services.content.release import ContentReleaseService
from app.services.content.review import ContentReviewService
from app.services.content.search import BraveSearchProvider, SearchProvider
from app.services.content.store import ContentStore
from app.services.content.video_adapter import MoneyPrinterVideoAdapter, VideoGenerator
from app.services.publishers.base import DraftPublisher
from app.services import llm


class ContentWorkflow:
    def __init__(
        self,
        store: ContentStore | None = None,
        blog_generator: BlogGenerator | None = None,
        researcher: SourceResearcher | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        video_generator: VideoGenerator | None = None,
        newsletter_generator: NewsletterGenerator | None = None,
        social_generator: SocialGenerator | None = None,
        consistency_checker: ContentConsistencyChecker | None = None,
        review_service: ContentReviewService | None = None,
        release_service: ContentReleaseService | None = None,
        publication_service: ContentPublicationService | None = None,
        search_provider: SearchProvider | None = None,
        external_distribution_service: ContentExternalDistributionService | None = None,
    ):
        self.store = store or ContentStore()
        self.blog_generator = blog_generator or BlogGenerator()
        self.researcher = researcher or SourceResearcher()
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        self.video_generator = video_generator or MoneyPrinterVideoAdapter()
        self.newsletter_generator = newsletter_generator or NewsletterGenerator()
        self.social_generator = social_generator or SocialGenerator()
        self.consistency_checker = consistency_checker or ContentConsistencyChecker()
        self.review_service = review_service or ContentReviewService()
        self.release_service = release_service or ContentReleaseService(
            review_service=self.review_service
        )
        self.publication_service = publication_service or ContentPublicationService(
            review_service=self.review_service
        )
        self.search_provider = search_provider or BraveSearchProvider()
        self.external_distribution_service = (
            external_distribution_service or ContentExternalDistributionService(
                review_service=self.review_service
            )
        )

    def create_project(self, request: ContentProjectCreate) -> ContentProject:
        return self.store.save(ContentProject(**request.model_dump()))

    def research_evidence(
        self,
        project_id: str,
        request: ResearchRequest,
        preserve_discovery: bool = False,
    ) -> ContentProject:
        project = self.store.get(project_id)
        if self._has_channel_output(project):
            raise ValueError(
                "evidence cannot be replaced after a channel output exists; "
                "create a new content project instead"
            )
        project.evidence_status = EvidenceStatus.researching
        if not preserve_discovery:
            project.source_discovery = None
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

    def discover_evidence(
        self, project_id: str, request: SourceDiscoveryRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        if self._has_channel_output(project):
            raise ValueError(
                "sources cannot be discovered after a channel output exists; "
                "create a new content project instead"
            )
        project.evidence_status = EvidenceStatus.researching
        project.last_error = None
        self.store.save(project)
        try:
            discovery = self.search_provider.search(request, project.topic)
        except Exception as exc:
            project.evidence_status = EvidenceStatus.failed
            project.last_error = str(exc)
            self.store.save(project)
            raise
        project.source_discovery = discovery
        self.store.save(project)
        sources = [
            SourceInput(url=candidate.url, title=candidate.title)
            for candidate in discovery.candidates
        ]
        return self.research_evidence(
            project_id,
            ResearchRequest(sources=sources),
            preserve_discovery=True,
        )

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
        self._invalidate_downstream(project, "blog output generation started")
        project.blog_status = BlogStatus.generating
        project.last_error = None
        project.channel_runs[ContentChannel.blog.value] = ChannelRunRecord(
            status=ChannelRunStatus.running,
            started_at=utc_now(),
            llm_usage=self._llm_usage(),
        )
        self.store.save(project)
        try:
            with llm.capture_usage() as usage_records:
                project.blog_output = self.blog_generator.generate(project)
            project.blog_status = BlogStatus.draft_complete
            project.ghost_status = GhostStatus.ready
            blog_run = project.channel_runs[ContentChannel.blog.value]
            blog_run.llm_usage = self._llm_usage(usage_records)
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
    def _llm_usage(records: list[dict] | None = None) -> LLMUsageRecord:
        if records:
            reported = [record for record in records if record["measurement"] == "reported"]
            providers = {record["provider"] for record in records}
            models = {record["model"] for record in records}
            return LLMUsageRecord(
                request_count=len(records),
                provider=providers.pop() if len(providers) == 1 else "multiple",
                model=models.pop() if len(models) == 1 else "multiple",
                input_tokens=(
                    sum(record["input_tokens"] or 0 for record in records)
                    if reported
                    else None
                ),
                output_tokens=(
                    sum(record["output_tokens"] or 0 for record in records)
                    if reported
                    else None
                ),
                measurement="reported" if len(reported) == len(records) else "unavailable",
                note=(
                    "Token counts are reported by the configured provider; pricing "
                    "is not inferred."
                    if len(reported) == len(records)
                    else "At least one provider response did not report token usage."
                ),
            )
        return LLMUsageRecord(
            request_count=1,
            measurement="unavailable",
            note=(
                "The current MoneyPrinterTurbo LLM adapter does not expose token "
                "usage or provider pricing, so cost is not fabricated."
            ),
        )

    @staticmethod
    def _generate_with_usage(func, args):
        with llm.capture_usage() as records:
            output = func(*args)
        return output, records

    def run_fanout(
        self, project_id: str, request: ContentFanoutRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        if (
            project.evidence_status != EvidenceStatus.approved
            or project.evidence_pack is None
        ):
            raise ValueError("approve an EvidencePack before running channel fan-out")

        previous_channels = list(project.requested_channels)
        project.requested_channels = request.channels
        project.last_error = None
        operations = {}
        snapshot = project.model_copy(deep=True)
        started_at = utc_now()

        should_generate_blog = ContentChannel.blog in request.channels and (
            project.blog_output is None or request.regenerate
        )
        if should_generate_blog:
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
            VideoStatus.planning,
            VideoStatus.queued,
            VideoStatus.rendering,
        }
        if (
            ContentChannel.short_video in request.channels
            and request.regenerate
            and video_active
        ):
            raise ValueError("an active video task cannot be regenerated")
        should_generate_video = (
            ContentChannel.short_video in request.channels
            and not video_active
            and (
                project.video_output is None
                or project.video_status == VideoStatus.failed
                or request.regenerate
            )
        )
        if should_generate_video:
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

        for channel, output, generator in (
            (
                ContentChannel.newsletter,
                project.newsletter_output,
                self.newsletter_generator,
            ),
            (ContentChannel.social, project.social_output, self.social_generator),
        ):
            if channel in request.channels and (output is None or request.regenerate):
                if channel == ContentChannel.newsletter:
                    project.newsletter_status = BlogStatus.generating
                else:
                    project.social_status = BlogStatus.generating
                project.channel_runs[channel.value] = ChannelRunRecord(
                    status=ChannelRunStatus.running,
                    started_at=started_at,
                    llm_usage=self._llm_usage(),
                )
                operations[channel] = (generator.generate, (snapshot,))

        if operations or previous_channels != request.channels:
            self._invalidate_downstream(
                project, "channel selection or output generation changed"
            )
        self.store.save(project)
        if not operations:
            if previous_channels != request.channels:
                project.consistency_report = self.consistency_checker.check(project)
            return self.store.save(project)

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            future_channels = {
                executor.submit(self._generate_with_usage, func, args): channel
                for channel, (func, args) in operations.items()
            }
            for future in as_completed(future_channels):
                channel = future_channels[future]
                run = project.channel_runs[channel.value]
                try:
                    output, usage_records = future.result()
                except Exception as exc:
                    run.status = ChannelRunStatus.failed
                    run.finished_at = utc_now()
                    run.error = str(exc)[:1000]
                    project.last_error = f"{channel.value}: {exc}"
                    if channel == ContentChannel.blog:
                        project.blog_status = BlogStatus.failed
                    elif channel == ContentChannel.short_video:
                        project.video_status = VideoStatus.failed
                    elif channel == ContentChannel.newsletter:
                        project.newsletter_status = BlogStatus.failed
                    else:
                        project.social_status = BlogStatus.failed
                else:
                    run.llm_usage = self._llm_usage(usage_records)
                    if channel == ContentChannel.blog:
                        project.blog_output = output
                        project.blog_status = BlogStatus.draft_complete
                        project.ghost_status = GhostStatus.ready
                        run.status = ChannelRunStatus.complete
                        run.finished_at = utc_now()
                    elif channel == ContentChannel.short_video:
                        project.video_output = output
                        project.video_status = VideoStatus.queued
                        run.status = ChannelRunStatus.queued
                    elif channel == ContentChannel.newsletter:
                        project.newsletter_output = output
                        project.newsletter_status = BlogStatus.draft_complete
                        run.status = ChannelRunStatus.complete
                        run.finished_at = utc_now()
                    else:
                        project.social_output = output
                        project.social_status = BlogStatus.draft_complete
                        run.status = ChannelRunStatus.complete
                        run.finished_at = utc_now()
                self.store.save(project)

        project.consistency_report = self.consistency_checker.check(project)
        return self.store.save(project)

    def refresh_video(self, project_id: str) -> ContentProject:
        project = self.store.get(project_id)
        if project.video_output is None:
            raise ValueError("start a video channel before refreshing its status")
        previous_status = project.video_status
        previous_output = project.video_output.model_dump(mode="json")
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
        output_changed = output.model_dump(mode="json") != previous_output
        if output_changed:
            self._invalidate_downstream(project, "video output changed")
        if output_changed or status != previous_status:
            project.consistency_report = self.consistency_checker.check(project)
        return self.store.save(project)

    def review_project(
        self, project_id: str, request: ContentReviewRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.review_service.review(project, request)
        if project.approval_status.value == "changes_requested":
            self.release_service.invalidate(project, "reviewer requested changes")
        project.last_error = None
        return self.store.save(project)

    def create_release_plan(
        self, project_id: str, request: ContentReleaseRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.release_service.create(project, request)
        project.last_error = None
        return self.store.save(project)

    def record_publication(
        self, project_id: str, request: PublicationReceiptRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.publication_service.record(project, request)
        project.last_error = None
        return self.store.save(project)

    def observe_publication(
        self,
        project_id: str,
        receipt_id: str,
        request: PublicationObservationRequest,
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.publication_service.observe(project, receipt_id, request)
        project.last_error = None
        return self.store.save(project)

    def publish_external_video(
        self, project_id: str, request: ExternalVideoPublishRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.external_distribution_service.publish_video(project, request)
        project.last_error = None
        return self.store.save(project)

    def refresh_external_publication(
        self, project_id: str, job_id: str
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.external_distribution_service.refresh(project, job_id)
        project.last_error = None
        return self.store.save(project)

    def refresh_external_analytics(
        self, project_id: str, job_id: str
    ) -> ContentProject:
        project = self.store.get(project_id)
        project = self.external_distribution_service.refresh_analytics(
            project, job_id
        )
        project.last_error = None
        return self.store.save(project)

    def sync_ghost_draft(
        self, project_id: str, publisher: DraftPublisher
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.blog_output is None:
            raise ValueError("generate a blog draft before creating a Ghost draft")
        self.review_service.require_current_channel_approval(
            project, ContentChannel.blog
        )
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

    def publish_ghost(
        self,
        project_id: str,
        request: GhostPublicationRequest,
        publisher,
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.blog_output is None or project.ghost_publication is None:
            raise ValueError("create an approved Ghost draft before publication")
        self.review_service.require_current_channel_approval(
            project, ContentChannel.blog
        )
        release = next(
            (plan for plan in project.release_plans if plan.release_id == request.release_id),
            None,
        )
        if release is None or release.status != ReleasePlanStatus.ready:
            raise ValueError("a matching ready release plan is required")
        if ContentChannel.blog not in release.channels:
            raise ValueError("the release plan does not include blog")
        project.ghost_status = GhostStatus.publishing
        self.store.save(project)
        try:
            project.ghost_publication = publisher.publish(
                project.blog_output,
                project.ghost_publication,
                request.scheduled_for if request.action == "schedule" else None,
            )
            project.ghost_status = (
                GhostStatus.scheduled
                if request.action == "schedule"
                else GhostStatus.published
            )
            project.last_error = None
        except Exception as exc:
            project.ghost_status = GhostStatus.failed
            project.last_error = str(exc)
            self.store.save(project)
            raise
        return self.store.save(project)

    def _invalidate_downstream(self, project: ContentProject, reason: str) -> None:
        self.review_service.invalidate(project, reason)
        self.release_service.invalidate(project, reason)

    @staticmethod
    def _has_channel_output(project: ContentProject) -> bool:
        return any(
            output is not None
            for output in (
                project.blog_output,
                project.video_output,
                project.newsletter_output,
                project.social_output,
            )
        )
