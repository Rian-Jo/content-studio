import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.models.content import ContentProject, utc_now
from app.utils import utils


class ContentProjectNotFoundError(LookupError):
    pass


class ContentStore:
    """SQLite-backed project snapshots with no API credentials in the payload."""

    def __init__(self, database_path: str | Path | None = None):
        default_path = Path(utils.storage_dir("content", create=True)) / "content.db"
        self.database_path = Path(database_path) if database_path else default_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS content_projects (
                    project_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_content_projects_updated_at "
                "ON content_projects(updated_at DESC)"
            )

    def save(self, project: ContentProject) -> ContentProject:
        project.updated_at = utc_now()
        payload = project.model_dump_json()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO content_projects(project_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    project.project_id,
                    payload,
                    project.created_at.isoformat(),
                    project.updated_at.isoformat(),
                ),
            )
        return project

    def get(self, project_id: str) -> ContentProject:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM content_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ContentProjectNotFoundError(project_id)
        return ContentProject.model_validate_json(row["payload"])

    def list(self, limit: int = 50, offset: int = 0) -> list[ContentProject]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM content_projects "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [ContentProject.model_validate_json(row["payload"]) for row in rows]
