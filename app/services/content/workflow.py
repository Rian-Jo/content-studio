from app.models.content import (
    BlogStatus,
    ContentProject,
    ContentProjectCreate,
    GhostStatus,
    utc_now,
)
from app.models.evidence import EvidenceStatus, ResearchRequest
from app.services.content.blog_generator import BlogGenerator
from app.services.content.evidence_builder import EvidenceBuilder
from app.services.content.research import SourceResearcher
from app.services.content.store import ContentStore
from app.services.publishers.base import DraftPublisher


class ContentWorkflow:
    def __init__(
        self,
        store: ContentStore | None = None,
        blog_generator: BlogGenerator | None = None,
        researcher: SourceResearcher | None = None,
        evidence_builder: EvidenceBuilder | None = None,
    ):
        self.store = store or ContentStore()
        self.blog_generator = blog_generator or BlogGenerator()
        self.researcher = researcher or SourceResearcher()
        self.evidence_builder = evidence_builder or EvidenceBuilder()

    def create_project(self, request: ContentProjectCreate) -> ContentProject:
        return self.store.save(ContentProject(**request.model_dump()))

    def research_evidence(
        self, project_id: str, request: ResearchRequest
    ) -> ContentProject:
        project = self.store.get(project_id)
        if project.blog_output is not None:
            raise ValueError(
                "evidence cannot be replaced after a blog draft exists; "
                "create a new content project instead"
            )
        project.evidence_status = EvidenceStatus.researching
        project.last_error = None
        self.store.save(project)
        try:
            sources = self.researcher.research(request.sources)
            project.evidence_pack = self.evidence_builder.build(project, sources)
            project.evidence_status = EvidenceStatus.ready_for_review
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
        self.store.save(project)
        try:
            project.blog_output = self.blog_generator.generate(project)
            project.blog_status = BlogStatus.draft_complete
            project.ghost_status = GhostStatus.ready
        except Exception as exc:
            project.blog_status = BlogStatus.failed
            project.last_error = str(exc)
            self.store.save(project)
            raise
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
