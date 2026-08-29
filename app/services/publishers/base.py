from typing import Protocol

from app.models.content import BlogOutput, GhostPublication


class DraftPublisher(Protocol):
    def sync_draft(
        self, blog: BlogOutput, existing: GhostPublication | None = None
    ) -> GhostPublication: ...
