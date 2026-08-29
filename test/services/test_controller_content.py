import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.controllers.v1 import content as content_controller
from app.models.content import ContentProjectCreate
from app.services.content import (
    BlogGenerator,
    ContentPublicationService,
    ContentReleaseService,
    ContentStore,
    ContentWorkflow,
)

from test.services.test_content_studio import (
    FakeEvidenceBuilder,
    FakeResearcher,
    FakePublicationVerifier,
    FakeVideoGenerator,
    sample_blog_payload,
)


class TestContentControllerHTTP(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = ContentStore(Path(self.temp_dir.name) / "api-content.db")
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
        self.original_workflow = content_controller._workflow
        content_controller._workflow = self.workflow
        self.client = TestClient(asgi.app)

    def tearDown(self):
        content_controller._workflow = self.original_workflow
        self.temp_dir.cleanup()

    def test_create_generate_and_read_project(self):
        created = self.client.post(
            "/api/v1/content/projects",
            json={
                "topic": "Content Studio",
                "audience": "builders",
                "objective": "explain",
                "language": "en",
                "requested_channels": ["blog"],
            },
        )
        self.assertEqual(created.status_code, 200)
        project_id = created.json()["data"]["project_id"]

        blocked = self.client.post(f"/api/v1/content/projects/{project_id}/blog")
        researched = self.client.post(
            f"/api/v1/content/projects/{project_id}/research",
            json={"sources": [{"url": "https://example.com/research"}]},
        )
        approved = self.client.post(
            f"/api/v1/content/projects/{project_id}/evidence/approve",
            json={"note": "Reviewed in the API test"},
        )
        generated = self.client.post(
            f"/api/v1/content/projects/{project_id}/blog"
        )
        fetched = self.client.get(f"/api/v1/content/projects/{project_id}")

        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(
            researched.json()["data"]["evidence_status"], "ready_for_review"
        )
        self.assertEqual(approved.json()["data"]["evidence_status"], "approved")
        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.json()["data"]["blog_status"], "draft_complete")
        self.assertEqual(fetched.json()["data"]["blog_output"]["slug"], "useful-guide")

    def test_fanout_and_video_refresh_endpoints(self):
        created = self.client.post(
            "/api/v1/content/projects",
            json={"topic": "Independent fan-out", "requested_channels": ["blog"]},
        )
        project_id = created.json()["data"]["project_id"]
        self.client.post(
            f"/api/v1/content/projects/{project_id}/research",
            json={"sources": [{"url": "https://example.com/research"}]},
        )
        self.client.post(
            f"/api/v1/content/projects/{project_id}/evidence/approve",
            json={},
        )

        fanout = self.client.post(
            f"/api/v1/content/projects/{project_id}/fanout",
            json={
                "channels": ["blog", "short_video"],
                "video_options": {"voice_name": "test-voice"},
            },
        )
        refreshed = self.client.post(
            f"/api/v1/content/projects/{project_id}/video/refresh"
        )
        reviewed = self.client.post(
            f"/api/v1/content/projects/{project_id}/review",
            json={"decision": "approve"},
        )
        release = self.client.post(
            f"/api/v1/content/projects/{project_id}/release-plans",
            json={"channels": ["blog", "short_video"]},
        )
        release_id = release.json()["data"]["release_plans"][-1]["release_id"]
        publication = self.client.post(
            f"/api/v1/content/projects/{project_id}/publications",
            json={
                "release_id": release_id,
                "channel": "blog",
                "platform": "ghost",
                "public_url": "https://example.com/published",
                "published_at": "2026-08-30T09:00:00+09:00",
            },
        )
        receipt_id = publication.json()["data"]["publication_receipts"][-1][
            "receipt_id"
        ]
        observation = self.client.post(
            f"/api/v1/content/projects/{project_id}/publications/"
            f"{receipt_id}/observations",
            json={"views": 100, "likes": 8, "clicks": 14},
        )

        self.assertEqual(fanout.status_code, 200)
        self.assertEqual(fanout.json()["data"]["video_status"], "queued")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["data"]["video_status"], "complete")
        self.assertEqual(
            refreshed.json()["data"]["blog_status"], "draft_complete"
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["data"]["approval_status"], "approved")
        self.assertEqual(release.status_code, 200)
        release_plan = release.json()["data"]["release_plans"][-1]
        self.assertFalse(release_plan["external_actions_performed"])
        self.assertTrue(
            (self.release_service.storage_root / release_plan["archive_path"]).is_file()
        )
        self.assertEqual(publication.status_code, 200)
        self.assertEqual(observation.status_code, 200)
        latest_observation = observation.json()["data"]["publication_receipts"][-1][
            "observations"
        ][-1]
        self.assertEqual(latest_observation["views"], 100)
        self.assertEqual(latest_observation["metrics_source"], "manual")

    def test_request_changes_requires_note(self):
        project = self.workflow.create_project(
            ContentProjectCreate(topic="Review validation")
        )

        response = self.client.post(
            f"/api/v1/content/projects/{project.project_id}/review",
            json={"decision": "request_changes"},
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_ghost_environment_never_calls_external_publisher(self):
        project = self.workflow.create_project(
            ContentProjectCreate(topic="No accidental publishing")
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("GHOST_ADMIN_")}

        with patch.dict(os.environ, env, clear=True), patch.object(
            content_controller, "GhostPublisher"
        ) as publisher:
            response = self.client.post(
                f"/api/v1/content/projects/{project.project_id}/ghost-draft"
            )

        self.assertEqual(response.status_code, 503)
        publisher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
