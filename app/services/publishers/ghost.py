import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

from app.models.content import BlogOutput, GhostPublication


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class GhostPublisherConfig:
    admin_url: str
    admin_api_key: str
    api_version: str = "v6.0"
    timeout_seconds: int = 30

    def __post_init__(self):
        if not self.admin_url.startswith(("https://", "http://")):
            raise ValueError("Ghost admin URL must use http or https")
        key_parts = self.admin_api_key.split(":", 1)
        if len(key_parts) != 2 or not all(key_parts):
            raise ValueError("Ghost Admin API key must use the id:secret format")
        try:
            bytes.fromhex(key_parts[1])
        except ValueError as exc:
            raise ValueError("Ghost Admin API secret must be hexadecimal") from exc


class GhostPublisher:
    """Server-side Ghost Admin API client with explicit status transitions."""

    def __init__(self, config: GhostPublisherConfig, session=None):
        self.config = config
        self._session = session or requests.Session()

    def _token(self) -> str:
        key_id, secret = self.config.admin_api_key.split(":", 1)
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
        payload = {"iat": now, "exp": now + 300, "aud": "/admin/"}
        encoded_header = _base64url(
            json.dumps(header, separators=(",", ":")).encode("utf-8")
        )
        encoded_payload = _base64url(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        unsigned = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(bytes.fromhex(secret), unsigned, hashlib.sha256).digest()
        return f"{unsigned.decode('ascii')}.{_base64url(signature)}"

    def _api_base(self) -> str:
        url = self.config.admin_url.rstrip("/") + "/"
        if url.endswith("/ghost/api/admin/"):
            return url
        if url.endswith("/ghost/"):
            return urljoin(url, "api/admin/")
        return urljoin(url, "ghost/api/admin/")

    def sync_draft(
        self, blog: BlogOutput, existing: GhostPublication | None = None
    ) -> GhostPublication:
        post = {
            "title": blog.title,
            "slug": blog.slug,
            "custom_excerpt": blog.excerpt,
            "html": blog.html,
            "meta_title": blog.seo_title,
            "meta_description": blog.meta_description,
            "tags": [{"name": tag} for tag in blog.tags],
            "status": "draft",
        }
        if blog.feature_image:
            post["feature_image"] = blog.feature_image

        method = "POST"
        endpoint = "posts/?source=html"
        if existing:
            method = "PUT"
            endpoint = f"posts/{existing.post_id}/?source=html"
            post["updated_at"] = existing.updated_at

        response = self._session.request(
            method,
            urljoin(self._api_base(), endpoint),
            headers={
                "Authorization": f"Ghost {self._token()}",
                "Accept-Version": self.config.api_version,
                "Content-Type": "application/json",
            },
            json={"posts": [post]},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        posts = response.json().get("posts", [])
        if not posts:
            raise ValueError("Ghost returned no post after draft synchronization")
        result = posts[0]
        return GhostPublication(
            post_id=result["id"],
            updated_at=result["updated_at"],
            status="draft",
            url=result.get("url"),
        )

    def publish(
        self,
        blog: BlogOutput,
        existing: GhostPublication,
        scheduled_for=None,
    ) -> GhostPublication:
        if existing.status != "draft":
            raise ValueError("only an existing Ghost draft can be published or scheduled")
        status = "scheduled" if scheduled_for is not None else "published"
        post = {
            "title": blog.title,
            "slug": blog.slug,
            "custom_excerpt": blog.excerpt,
            "html": blog.html,
            "meta_title": blog.seo_title,
            "meta_description": blog.meta_description,
            "tags": [{"name": tag} for tag in blog.tags],
            "status": status,
            "updated_at": existing.updated_at,
        }
        if blog.feature_image:
            post["feature_image"] = blog.feature_image
        if scheduled_for is not None:
            post["published_at"] = scheduled_for.isoformat()
        response = self._session.request(
            "PUT",
            urljoin(self._api_base(), f"posts/{existing.post_id}/?source=html"),
            headers={
                "Authorization": f"Ghost {self._token()}",
                "Accept-Version": self.config.api_version,
                "Content-Type": "application/json",
            },
            json={"posts": [post]},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        posts = response.json().get("posts", [])
        if not posts:
            raise ValueError("Ghost returned no post after publication transition")
        result = posts[0]
        return GhostPublication(
            post_id=result["id"],
            updated_at=result["updated_at"],
            status=status,
            url=result.get("url"),
            published_at=result.get("published_at"),
        )
