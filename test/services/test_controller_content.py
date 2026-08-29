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
from app.services.content import BlogGenerator, ContentStore, ContentWorkflow

from test.services.test_content_studio import sample_blog_payload


class TestContentControllerHTTP(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = ContentStore(Path(self.temp_dir.name) / "api-content.db")
        generator = BlogGenerator(
            responder=lambda _: json.dumps(sample_blog_payload())
        )
        self.workflow = ContentWorkflow(store=store, blog_generator=generator)
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

        generated = self.client.post(
            f"/api/v1/content/projects/{project_id}/blog"
        )
        fetched = self.client.get(f"/api/v1/content/projects/{project_id}")

        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.json()["data"]["blog_status"], "draft_complete")
        self.assertEqual(fetched.json()["data"]["blog_output"]["slug"], "useful-guide")

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
