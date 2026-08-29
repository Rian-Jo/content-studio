import base64
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.models.content import (
    BlogOutput,
    BlogStatus,
    ChannelRunRecord,
    ChannelRunStatus,
    ConsistencyStatus,
    ContentChannel,
    ContentFanoutRequest,
    ContentProject,
    ContentProjectCreate,
    ContentReleaseRequest,
    ContentReviewRequest,
    GhostPublication,
    GhostStatus,
    PublicationObservation,
    PublicationObservationRequest,
    PublicationReachability,
    PublicationReceiptRequest,
    ReviewDecision,
    VideoGenerationOptions,
    VideoOutput,
    VideoStatus,
)
from app.models.evidence import (
    ClaimRecord,
    EvidencePack,
    EvidenceStatus,
    ResearchRequest,
    SourceInput,
    SourceRecord,
    SourceVerificationStatus,
)
from app.services.content.blog_generator import BlogGenerator
from app.services.content.evidence_builder import EvidenceBuilder
from app.services.content.quality_gate import ContentConsistencyChecker
from app.services.content.publication import (
    ContentPublicationService,
    PublicationUrlVerifier,
)
from app.services.content.research import SourceResearcher, ensure_public_http_url
from app.services.content.release import ContentReleaseService
from app.services.content.store import ContentProjectNotFoundError, ContentStore
from app.services.content.video_adapter import MoneyPrinterVideoAdapter
from app.services.content.video_generator import VideoPlanGenerator
from app.services.content.workflow import ContentWorkflow
from app.services.publishers.ghost import GhostPublisher, GhostPublisherConfig
from app.services.state import MemoryState


def sample_blog_payload() -> dict:
    markdown = (
        "# Useful Guide\n\nThis introduction explains the practical problem clearly.\n\n"
        "## Steps\n\nFollow these concrete steps and verify each result before continuing.\n\n"
        "## Conclusion\n\nReview the draft before publishing it to any external service."
    )
    html = (
        "<h1>Useful Guide</h1><p>This introduction explains the practical problem "
        "clearly.</p><h2>Steps</h2><p>Follow these concrete steps and verify each "
        "result before continuing.</p><h2>Conclusion</h2><p>Review the draft before "
        "publishing it to any external service.</p>"
    )
    return {
        "title": "Useful Guide",
        "slug": "useful-guide",
        "excerpt": "A practical guide for careful implementation.",
        "markdown": markdown,
        "html": html,
        "seo_title": "Useful Guide for Careful Implementation",
        "meta_description": "Create and review a useful implementation guide before publishing.",
        "tags": ["automation", "guide"],
        "feature_image": None,
        "evidence_claim_ids": ["claim-1"],
    }


def sample_source(source_id: str = "source-1") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        requested_url="https://example.com/research",
        final_url="https://example.com/research",
        title="Verified Research",
        publisher="example.com",
        verification_status=SourceVerificationStatus.verified,
        http_status=200,
        content_type="text/html",
        content_excerpt=(
            "This verified source explains the subject with enough detail for "
            "a traceable claim and an independently reviewable evidence record."
        ),
        content_sha256="a" * 64,
        checked_at=datetime.now(timezone.utc),
    )


def sample_evidence_payload(source_id: str = "source-1") -> dict:
    return {
        "claims": [
            {
                "statement": "The source supports a traceable content claim.",
                "source_ids": [source_id],
                "confidence": "high",
            }
        ],
        "key_messages": ["Use reviewed evidence before drafting."],
        "counterpoints": ["One source does not establish broad consensus."],
        "seo_keywords": ["evidence based content"],
    }


def approved_project() -> ContentProject:
    source = sample_source()
    evidence = EvidenceBuilder(
        responder=lambda _: json.dumps(sample_evidence_payload(source.source_id))
    ).build(ContentProject(topic="Test"), [source])
    evidence.claims[0].claim_id = "claim-1"
    evidence.approved_at = datetime.now(timezone.utc)
    return ContentProject(
        topic="Test",
        evidence_pack=evidence,
        evidence_status=EvidenceStatus.approved,
    )


def sample_video_payload(claim_id: str = "claim-1") -> dict:
    return {
        "title": "Evidence Video",
        "hook": "What makes a content claim trustworthy?",
        "narration": (
            "A useful content workflow connects each factual claim to reviewed "
            "evidence before it creates a channel-specific draft."
        ),
        "scenes": [
            {
                "narration": "Connect each claim to reviewed evidence.",
                "visual_direction": "Show a claim linked to a source card.",
            }
        ],
        "search_terms": ["evidence based content workflow"],
        "caption": "Build content from reviewed evidence.",
        "hashtags": ["content", "evidence"],
        "evidence_claim_ids": [claim_id],
    }


class FakeEvidenceBuilder:
    def build(self, project, sources):
        return EvidencePack(
            sources=sources,
            claims=[
                ClaimRecord(
                    claim_id="claim-1",
                    statement="The source supports a traceable content claim.",
                    source_ids=[sources[0].source_id],
                    confidence="high",
                )
            ],
            key_messages=["Use reviewed evidence before drafting."],
            counterpoints=["One source does not establish broad consensus."],
            seo_keywords=["evidence based content"],
            checked_at=datetime.now(timezone.utc),
        )


class TestContentStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ContentStore(Path(self.temp_dir.name) / "content.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_project_round_trip_and_update(self):
        project = ContentProject(topic="Independent blog workflow")
        self.store.save(project)
        project.blog_status = BlogStatus.generating
        self.store.save(project)

        stored = self.store.get(project.project_id)

        self.assertEqual(stored.topic, "Independent blog workflow")
        self.assertEqual(stored.blog_status, BlogStatus.generating)
        self.assertEqual(self.store.list()[0].project_id, project.project_id)

    def test_missing_project_raises_stable_error(self):
        with self.assertRaises(ContentProjectNotFoundError):
            self.store.get("missing")


class TestBlogGenerator(unittest.TestCase):
    def test_extracts_structured_blog_from_fenced_response(self):
        response = "```json\n" + json.dumps(sample_blog_payload()) + "\n```"
        generator = BlogGenerator(responder=lambda _: response)

        output = generator.generate(approved_project())

        self.assertEqual(output.slug, "useful-guide")
        self.assertIn("<h1>", output.html)

    def test_rejects_unstructured_response(self):
        generator = BlogGenerator(responder=lambda _: "plain prose")

        with self.assertRaisesRegex(ValueError, "JSON object"):
            generator.generate(approved_project())

    def test_requires_approved_evidence(self):
        generator = BlogGenerator(responder=lambda _: json.dumps(sample_blog_payload()))

        with self.assertRaisesRegex(ValueError, "approve an EvidencePack"):
            generator.generate(ContentProject(topic="Test"))


class TestVideoPlanGenerator(unittest.TestCase):
    def test_builds_video_plan_from_approved_claims(self):
        generator = VideoPlanGenerator(
            responder=lambda _: json.dumps(sample_video_payload())
        )

        output = generator.generate(approved_project())

        self.assertEqual(output.evidence_claim_ids, ["claim-1"])
        self.assertEqual(output.search_terms, ["evidence based content workflow"])

    def test_rejects_unknown_evidence_claim(self):
        generator = VideoPlanGenerator(
            responder=lambda _: json.dumps(sample_video_payload("unknown-claim"))
        )

        with self.assertRaisesRegex(ValueError, "unknown evidence claim"):
            generator.generate(approved_project())


class TestMoneyPrinterVideoAdapter(unittest.TestCase):
    def test_submits_existing_mpt_pipeline_without_cross_posting(self):
        calls = []
        state = MemoryState()
        adapter = MoneyPrinterVideoAdapter(
            plan_generator=VideoPlanGenerator(
                responder=lambda _: json.dumps(sample_video_payload())
            ),
            scheduler=lambda func, **kwargs: calls.append((func, kwargs)),
        )

        with patch("app.services.content.video_adapter.sm.state", state):
            output = adapter.generate(
                approved_project(),
                VideoGenerationOptions(
                    video_source="pexels", voice_name="test-voice"
                ),
            )

        _, kwargs = calls[0]
        self.assertFalse(kwargs["allow_cross_post"])
        self.assertEqual(kwargs["params"].video_script, output.narration)
        self.assertEqual(kwargs["params"].video_terms, output.search_terms)
        self.assertEqual(kwargs["params"].voice_name, "test-voice")

    def test_refresh_collects_completed_video_files(self):
        state = MemoryState()
        output = VideoOutput.model_validate(sample_video_payload())
        output.task_id = "video-task"
        state.update_task(
            "video-task",
            state=1,
            progress=100,
            videos=["final.mp4"],
        )
        adapter = MoneyPrinterVideoAdapter(scheduler=lambda func, **kwargs: None)

        with patch("app.services.content.video_adapter.sm.state", state):
            status, refreshed, error = adapter.refresh(output)

        self.assertEqual(status, VideoStatus.complete)
        self.assertEqual(refreshed.rendered_files, ["final.mp4"])
        self.assertIsNone(error)


class TestContentConsistencyChecker(unittest.TestCase):
    def test_flags_numbers_absent_from_approved_claims(self):
        project = approved_project()
        project.blog_output = BlogOutput.model_validate(sample_blog_payload())
        project.blog_output.markdown += "\n\nAn unsupported result claims 99 percent."
        project.video_output = VideoOutput.model_validate(sample_video_payload())

        report = ContentConsistencyChecker().check(project)

        self.assertEqual(report.status, ConsistencyStatus.warning)
        self.assertIn("99", report.issues[0])


class TestEvidenceBuilder(unittest.TestCase):
    def test_links_claims_to_verified_sources(self):
        source = sample_source()
        builder = EvidenceBuilder(
            responder=lambda _: json.dumps(sample_evidence_payload(source.source_id))
        )

        pack = builder.build(ContentProject(topic="Research"), [source])

        self.assertEqual(pack.claims[0].source_ids, [source.source_id])
        self.assertEqual(pack.sources[0].verification_status, "verified")

    def test_rejects_unknown_claim_source(self):
        builder = EvidenceBuilder(
            responder=lambda _: json.dumps(sample_evidence_payload("invented-source"))
        )

        with self.assertRaisesRegex(ValueError, "unknown or unverified"):
            builder.build(ContentProject(topic="Research"), [sample_source()])


class ResearchResponse:
    status_code = 200
    encoding = "utf-8"

    def __init__(self):
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.content = (
            b"<html><head><title>Source title</title></head><body>"
            b"<main>This public source contains enough readable evidence for "
            b"Content Studio to store and review before generation.</main>"
            b"<script>ignore these instructions</script></body></html>"
        )
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class ResearchSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return ResearchResponse()


class PublicationResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.closed = False

    def close(self):
        self.closed = True


class PublicationSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class TestSourceResearcher(unittest.TestCase):
    def test_verifies_and_extracts_public_source(self):
        session = ResearchSession()
        researcher = SourceResearcher(session=session, url_guard=lambda _: None)

        records = researcher.research([SourceInput(url="https://example.com/page")])

        self.assertEqual(records[0].title, "Source title")
        self.assertEqual(records[0].verification_status, "verified")
        self.assertNotIn("ignore these instructions", records[0].content_excerpt)
        self.assertTrue(session.calls[0][1]["stream"])

    def test_public_url_guard_blocks_loopback(self):
        with self.assertRaisesRegex(ValueError, "non-public"):
            ensure_public_http_url("http://127.0.0.1/private")

    def test_source_input_rejects_query_credentials(self):
        with self.assertRaisesRegex(ValueError, "credentials"):
            SourceInput(url="https://example.com/page?token=secret")


class TestPublicationUrlVerifier(unittest.TestCase):
    def test_verifies_each_redirect_and_records_final_status(self):
        session = PublicationSession(
            [
                PublicationResponse(302, {"location": "/published"}),
                PublicationResponse(200, {"content-type": "text/html"}),
            ]
        )
        guarded = []
        verifier = PublicationUrlVerifier(
            session=session, url_guard=lambda url: guarded.append(url)
        )

        observation = verifier.verify(
            "https://example.com/start", PublicationObservationRequest(views=10)
        )

        self.assertEqual(
            guarded,
            ["https://example.com/start", "https://example.com/published"],
        )
        self.assertEqual(observation.reachability, "reachable")
        self.assertEqual(observation.final_url, "https://example.com/published")
        self.assertEqual(observation.views, 10)

    def test_blocks_redirect_to_non_public_address_before_request(self):
        session = PublicationSession(
            [PublicationResponse(302, {"location": "http://127.0.0.1/private"})]
        )

        def guard(url):
            if "127.0.0.1" in url:
                raise ValueError("private or non-public source URLs are not allowed")

        verifier = PublicationUrlVerifier(session=session, url_guard=guard)

        with self.assertRaisesRegex(ValueError, "publication URLs"):
            verifier.verify(
                "https://example.com/start", PublicationObservationRequest()
            )

        self.assertEqual(len(session.calls), 1)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(
            {
                "posts": [
                    {
                        "id": "post-123",
                        "updated_at": "2026-08-30T01:02:03.000Z",
                        "status": "draft",
                        "url": "https://example.com/p/123",
                    }
                ]
            }
        )


class TestGhostPublisher(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.publisher = GhostPublisher(
            GhostPublisherConfig(
                admin_url="https://admin.example.com",
                admin_api_key="key-id:" + ("ab" * 32),
            ),
            session=self.session,
        )
        self.blog = BlogOutput.model_validate(sample_blog_payload())

    def test_creates_draft_with_server_side_jwt(self):
        publication = self.publisher.sync_draft(self.blog)

        method, url, kwargs = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url, "https://admin.example.com/ghost/api/admin/posts/?source=html"
        )
        self.assertEqual(kwargs["json"]["posts"][0]["status"], "draft")
        self.assertNotIn("published", kwargs["json"]["posts"][0])
        self.assertEqual(publication.post_id, "post-123")

        token = kwargs["headers"]["Authorization"].removeprefix("Ghost ")
        encoded_header = token.split(".", 1)[0]
        encoded_header += "=" * (-len(encoded_header) % 4)
        header = json.loads(base64.urlsafe_b64decode(encoded_header))
        self.assertEqual(header["kid"], "key-id")
        self.assertNotIn("ab" * 32, token)

    def test_updates_existing_draft_instead_of_creating_duplicate(self):
        existing = GhostPublication(
            post_id="post-123", updated_at="2026-08-29T00:00:00.000Z"
        )

        self.publisher.sync_draft(self.blog, existing)

        method, url, kwargs = self.session.calls[0]
        self.assertEqual(method, "PUT")
        self.assertIn("posts/post-123/", url)
        self.assertEqual(
            kwargs["json"]["posts"][0]["updated_at"], existing.updated_at
        )


class FakePublisher:
    def __init__(self):
        self.existing = None

    def sync_draft(self, blog, existing=None):
        self.existing = existing
        return GhostPublication(post_id="ghost-1", updated_at="now")


class FakeResearcher:
    def research(self, sources):
        return [sample_source()]


class FakeVideoGenerator:
    def __init__(self, error: str | None = None):
        self.error = error

    def generate(self, project, options):
        if self.error:
            raise ValueError(self.error)
        output = VideoOutput.model_validate(sample_video_payload())
        output.task_id = "mpt-video-1"
        return output

    def refresh(self, output):
        return (
            VideoStatus.complete,
            output.model_copy(update={"rendered_files": ["final.mp4"]}),
            None,
        )


class FakePublicationVerifier:
    def __init__(self):
        self.calls = []

    def verify(self, url, metrics):
        self.calls.append((url, metrics))
        return PublicationObservation(
            observed_at=datetime.now(timezone.utc),
            reachability=PublicationReachability.reachable,
            final_url=url,
            http_status=200,
            content_type="text/html",
            response_time_ms=12,
            **metrics.model_dump(),
        )


class FailingBlogGenerator:
    def generate(self, project):
        raise ValueError("blog channel failed")


class TestContentWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = ContentStore(Path(self.temp_dir.name) / "workflow.db")
        generator = BlogGenerator(
            responder=lambda _: json.dumps(sample_blog_payload())
        )
        self.release_service = ContentReleaseService(
            Path(self.temp_dir.name) / "releases"
        )
        self.publication_verifier = FakePublicationVerifier()
        self.workflow = ContentWorkflow(
            store=store,
            blog_generator=generator,
            researcher=FakeResearcher(),
            evidence_builder=FakeEvidenceBuilder(),
            video_generator=FakeVideoGenerator(),
            release_service=self.release_service,
            publication_service=ContentPublicationService(
                verifier=self.publication_verifier
            ),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blog_and_ghost_statuses_are_independent(self):
        project = self.workflow.create_project(
            ContentProjectCreate(topic="Independent pipelines")
        )
        researched = self.workflow.research_evidence(
            project.project_id,
            ResearchRequest(sources=[SourceInput(url="https://example.com/research")]),
        )
        self.assertEqual(researched.evidence_status, EvidenceStatus.ready_for_review)
        approved = self.workflow.approve_evidence(project.project_id, "Reviewed")
        self.assertEqual(approved.evidence_status, EvidenceStatus.approved)
        generated = self.workflow.generate_blog(project.project_id)

        self.assertEqual(generated.blog_status, BlogStatus.draft_complete)
        self.assertEqual(generated.ghost_status, GhostStatus.ready)

        reviewed = self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        self.assertEqual(reviewed.approval_status, "approved")

        synced = self.workflow.sync_ghost_draft(project.project_id, FakePublisher())

        self.assertEqual(synced.blog_status, BlogStatus.draft_complete)
        self.assertEqual(synced.ghost_status, GhostStatus.draft_created)
        self.assertEqual(synced.ghost_publication.post_id, "ghost-1")

    def test_blog_generation_is_blocked_until_evidence_is_approved(self):
        project = self.workflow.create_project(
            ContentProjectCreate(topic="Approval gate")
        )

        with self.assertRaisesRegex(ValueError, "approve an EvidencePack"):
            self.workflow.generate_blog(project.project_id)

        stored = self.workflow.store.get(project.project_id)
        self.assertEqual(stored.blog_status, BlogStatus.not_started)

    def test_fanout_runs_blog_and_video_from_one_evidence_pack(self):
        project = approved_project()
        self.workflow.store.save(project)

        result = self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(
                channels=["blog", "short_video"],
                video_options=VideoGenerationOptions(voice_name="test-voice"),
            ),
        )

        self.assertEqual(result.blog_status, BlogStatus.draft_complete)
        self.assertEqual(result.video_status, VideoStatus.queued)
        self.assertEqual(result.video_output.task_id, "mpt-video-1")
        self.assertEqual(
            result.channel_runs["blog"].status, ChannelRunStatus.complete
        )
        self.assertEqual(
            result.channel_runs["short_video"].status, ChannelRunStatus.queued
        )
        self.assertIsNone(
            result.channel_runs["blog"].llm_usage.estimated_cost_usd
        )
        self.assertEqual(
            result.consistency_report.status, ConsistencyStatus.passed
        )

    def test_blog_failure_does_not_cancel_video_channel(self):
        workflow = ContentWorkflow(
            store=self.workflow.store,
            blog_generator=FailingBlogGenerator(),
            video_generator=FakeVideoGenerator(),
        )
        project = approved_project()
        workflow.store.save(project)

        result = workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(
                channels=["blog", "short_video"],
                video_options=VideoGenerationOptions(voice_name="test-voice"),
            ),
        )

        self.assertEqual(result.blog_status, BlogStatus.failed)
        self.assertEqual(result.video_status, VideoStatus.queued)
        self.assertIsNotNone(result.video_output)

    def test_video_failure_does_not_cancel_blog_channel(self):
        workflow = ContentWorkflow(
            store=self.workflow.store,
            blog_generator=self.workflow.blog_generator,
            video_generator=FakeVideoGenerator(error="video channel failed"),
        )
        project = approved_project()
        workflow.store.save(project)

        result = workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(
                channels=["blog", "short_video"],
                video_options=VideoGenerationOptions(voice_name="test-voice"),
            ),
        )

        self.assertEqual(result.blog_status, BlogStatus.draft_complete)
        self.assertEqual(result.video_status, VideoStatus.failed)
        self.assertIsNotNone(result.blog_output)

    def test_refresh_video_preserves_blog_and_updates_video(self):
        project = approved_project()
        project.blog_output = BlogOutput.model_validate(sample_blog_payload())
        project.blog_status = BlogStatus.draft_complete
        project.video_output = VideoOutput.model_validate(sample_video_payload())
        project.video_output.task_id = "mpt-video-1"
        project.video_status = VideoStatus.queued
        project.channel_runs["short_video"] = ChannelRunRecord(
            status=ChannelRunStatus.queued,
            started_at=datetime.now(timezone.utc),
        )
        self.workflow.store.save(project)

        refreshed = self.workflow.refresh_video(project.project_id)

        self.assertEqual(refreshed.video_status, VideoStatus.complete)
        self.assertEqual(refreshed.video_output.rendered_files, ["final.mp4"])
        self.assertEqual(refreshed.blog_status, BlogStatus.draft_complete)

    def test_review_waits_for_selected_video_to_complete(self):
        project = approved_project()
        self.workflow.store.save(project)
        result = self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(
                channels=["blog", "short_video"],
                video_options=VideoGenerationOptions(voice_name="test-voice"),
            ),
        )

        with self.assertRaisesRegex(ValueError, "video channel is not ready"):
            self.workflow.review_project(
                result.project_id,
                ContentReviewRequest(decision=ReviewDecision.approve),
            )

        self.workflow.refresh_video(result.project_id)
        reviewed = self.workflow.review_project(
            result.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        self.assertEqual(reviewed.approval_status, "approved")
        self.assertEqual(len(reviewed.review_record.snapshot_sha256), 64)
        refreshed_again = self.workflow.refresh_video(result.project_id)
        self.assertTrue(
            self.workflow.review_service.is_current_approval(refreshed_again)
        )

    def test_quality_warning_requires_explicit_acknowledgement(self):
        project = approved_project()
        project.requested_channels = [
            ContentChannel.blog,
            ContentChannel.short_video,
        ]
        project.blog_output = BlogOutput.model_validate(sample_blog_payload())
        project.blog_output.markdown += "\n\nAn unsupported result claims 99 percent."
        project.blog_status = BlogStatus.draft_complete
        project.video_output = VideoOutput.model_validate(sample_video_payload())
        project.video_output.task_id = "mpt-video-1"
        project.video_status = VideoStatus.complete
        project.consistency_report = ContentConsistencyChecker().check(project)
        self.workflow.store.save(project)

        with self.assertRaisesRegex(ValueError, "acknowledge quality warnings"):
            self.workflow.review_project(
                project.project_id,
                ContentReviewRequest(decision=ReviewDecision.approve),
            )

        reviewed = self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(
                decision=ReviewDecision.approve,
                acknowledge_quality_warnings=True,
            ),
        )
        self.assertTrue(reviewed.review_record.quality_warnings_acknowledged)

    def test_regeneration_invalidates_previous_approval(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        approved = self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        self.assertEqual(approved.approval_status, "approved")

        regenerated = self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(channels=["blog"], regenerate=True),
        )

        self.assertEqual(regenerated.approval_status, "waiting_for_review")
        self.assertIsNotNone(regenerated.review_record.invalidated_at)

    def test_release_export_is_bound_to_approval_without_external_action(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        reviewed = self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )

        released = self.workflow.create_release_plan(
            project.project_id,
            ContentReleaseRequest(channels=[ContentChannel.blog]),
        )

        plan = released.release_plans[-1]
        archive_path = self.release_service.storage_root / plan.archive_path
        manifest_path = (
            self.release_service.storage_root / plan.bundle_path / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(archive_path) as archive:
            archived_names = set(archive.namelist())

        self.assertEqual(
            plan.approval_snapshot_sha256,
            reviewed.review_record.snapshot_sha256,
        )
        self.assertFalse(plan.external_actions_performed)
        self.assertFalse(manifest["external_actions_performed"])
        self.assertIn("blog/article.md", archived_names)
        self.assertIn("evidence.json", archived_names)
        self.assertIn("review.json", archived_names)
        self.assertIn("manifest.json", archived_names)
        self.assertTrue(archive_path.is_file())

    def test_release_export_requires_current_channel_approval(self):
        project = approved_project()
        self.workflow.store.save(project)

        with self.assertRaisesRegex(ValueError, "approve the current output"):
            self.workflow.create_release_plan(
                project.project_id,
                ContentReleaseRequest(channels=[ContentChannel.blog]),
            )

    def test_video_release_exports_plan_without_copying_rendered_media(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(
                channels=["blog", "short_video"],
                video_options=VideoGenerationOptions(voice_name="test-voice"),
            ),
        )
        self.workflow.refresh_video(project.project_id)
        self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )

        released = self.workflow.create_release_plan(
            project.project_id,
            ContentReleaseRequest(channels=[ContentChannel.short_video]),
        )

        plan = released.release_plans[-1]
        archive_path = self.release_service.storage_root / plan.archive_path
        video_plan_path = (
            self.release_service.storage_root
            / plan.bundle_path
            / "short-video"
            / "plan.json"
        )
        video_plan = json.loads(video_plan_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(archive_path) as archive:
            archived_names = set(archive.namelist())

        self.assertEqual(video_plan["rendered_files"], ["final.mp4"])
        self.assertFalse(video_plan["media_files_copied"])
        self.assertIn("short-video/plan.json", archived_names)
        self.assertNotIn("final.mp4", archived_names)

    def test_regeneration_marks_existing_release_plan_stale(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        self.workflow.create_release_plan(
            project.project_id,
            ContentReleaseRequest(channels=[ContentChannel.blog]),
        )

        regenerated = self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(channels=["blog"], regenerate=True),
        )

        self.assertEqual(regenerated.release_plans[-1].status, "stale")
        self.assertIsNotNone(regenerated.release_plans[-1].stale_at)

    def test_release_request_requires_timezone_for_planned_time(self):
        with self.assertRaisesRegex(ValueError, "timezone offset"):
            ContentReleaseRequest(
                channels=[ContentChannel.blog],
                planned_for=datetime(2026, 9, 1, 9, 0),
            )

    def test_records_publication_and_manual_metric_observations(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        released = self.workflow.create_release_plan(
            project.project_id,
            ContentReleaseRequest(channels=[ContentChannel.blog]),
        )
        plan = released.release_plans[-1]

        recorded = self.workflow.record_publication(
            project.project_id,
            PublicationReceiptRequest(
                release_id=plan.release_id,
                channel=ContentChannel.blog,
                platform="ghost",
                public_url="https://example.com/useful-guide",
                published_at=datetime.now(timezone.utc),
            ),
        )
        receipt = recorded.publication_receipts[-1]
        observed = self.workflow.observe_publication(
            project.project_id,
            receipt.receipt_id,
            PublicationObservationRequest(
                views=120, likes=12, comments=3, shares=4, clicks=20
            ),
        )

        receipt = observed.publication_receipts[-1]
        self.assertFalse(receipt.external_action_performed_by_studio)
        self.assertEqual(len(receipt.observations), 2)
        self.assertEqual(receipt.observations[-1].views, 120)
        self.assertEqual(receipt.observations[-1].metrics_source, "manual")
        self.assertEqual(len(self.publication_verifier.calls), 2)

    def test_stale_release_rejects_publication_receipt(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        released = self.workflow.create_release_plan(
            project.project_id,
            ContentReleaseRequest(channels=[ContentChannel.blog]),
        )
        release_id = released.release_plans[-1].release_id
        self.workflow.run_fanout(
            project.project_id,
            ContentFanoutRequest(channels=["blog"], regenerate=True),
        )

        with self.assertRaisesRegex(ValueError, "stale release"):
            self.workflow.record_publication(
                project.project_id,
                PublicationReceiptRequest(
                    release_id=release_id,
                    channel=ContentChannel.blog,
                    platform="other",
                    public_url="https://example.com/stale",
                    published_at=datetime.now(timezone.utc),
                ),
            )

    def test_publication_time_requires_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone offset"):
            PublicationReceiptRequest(
                release_id="release-1",
                channel=ContentChannel.blog,
                platform="other",
                public_url="https://example.com/article",
                published_at=datetime(2026, 9, 1, 9, 0),
            )

    def test_ghost_sync_rejects_changed_snapshot(self):
        project = approved_project()
        self.workflow.store.save(project)
        self.workflow.generate_blog(project.project_id)
        self.workflow.review_project(
            project.project_id,
            ContentReviewRequest(decision=ReviewDecision.approve),
        )
        changed = self.workflow.store.get(project.project_id)
        changed.blog_output.title = "Changed after approval"
        self.workflow.store.save(changed)

        with self.assertRaisesRegex(ValueError, "current output snapshot"):
            self.workflow.sync_ghost_draft(project.project_id, FakePublisher())

    def test_request_changes_requires_review_note(self):
        with self.assertRaisesRegex(ValueError, "review note"):
            ContentReviewRequest(decision=ReviewDecision.request_changes)


if __name__ == "__main__":
    unittest.main()
