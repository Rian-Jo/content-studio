import base64
import json
import tempfile
import unittest
from pathlib import Path

from app.models.content import (
    BlogOutput,
    BlogStatus,
    ContentProject,
    ContentProjectCreate,
    GhostPublication,
    GhostStatus,
)
from app.services.content.blog_generator import BlogGenerator
from app.services.content.store import ContentProjectNotFoundError, ContentStore
from app.services.content.workflow import ContentWorkflow
from app.services.publishers.ghost import GhostPublisher, GhostPublisherConfig


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
    }


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

        output = generator.generate(ContentProject(topic="Test"))

        self.assertEqual(output.slug, "useful-guide")
        self.assertIn("<h1>", output.html)

    def test_rejects_unstructured_response(self):
        generator = BlogGenerator(responder=lambda _: "plain prose")

        with self.assertRaisesRegex(ValueError, "JSON object"):
            generator.generate(ContentProject(topic="Test"))


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


class TestContentWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = ContentStore(Path(self.temp_dir.name) / "workflow.db")
        generator = BlogGenerator(
            responder=lambda _: json.dumps(sample_blog_payload())
        )
        self.workflow = ContentWorkflow(store=store, blog_generator=generator)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blog_and_ghost_statuses_are_independent(self):
        project = self.workflow.create_project(
            ContentProjectCreate(topic="Independent pipelines")
        )
        generated = self.workflow.generate_blog(project.project_id)

        self.assertEqual(generated.blog_status, BlogStatus.draft_complete)
        self.assertEqual(generated.ghost_status, GhostStatus.ready)

        synced = self.workflow.sync_ghost_draft(project.project_id, FakePublisher())

        self.assertEqual(synced.blog_status, BlogStatus.draft_complete)
        self.assertEqual(synced.ghost_status, GhostStatus.draft_created)
        self.assertEqual(synced.ghost_publication.post_id, "ghost-1")


if __name__ == "__main__":
    unittest.main()
