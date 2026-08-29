import json
from collections.abc import Callable

from app.models.content import BlogOutput, ContentProject
from app.services import llm


BLOG_SYSTEM_PROMPT = """
You are a senior editor creating a factual, useful blog draft. Return exactly one
JSON object and no prose outside it. Required keys: title, slug, excerpt, markdown,
html, seo_title, meta_description, tags, feature_image. The slug must contain only
lowercase ASCII letters, digits, and hyphens. Markdown and HTML must contain the
same article, with a clear introduction, useful headings, and a conclusion. Do not
invent sources, quotations, statistics, or first-hand experience. Set feature_image
to null unless an image URL was explicitly supplied.
""".strip()


def _default_responder(prompt: str) -> str:
    return llm._generate_response(prompt)


def _extract_json_object(response: str) -> dict:
    start = response.find("{")
    end = response.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("blog generator did not return a JSON object")
    try:
        payload = json.loads(response[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("blog generator returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("blog generator returned a non-object JSON value")
    return payload


class BlogGenerator:
    def __init__(self, responder: Callable[[str], str] | None = None):
        self._responder = responder or _default_responder

    def generate(self, project: ContentProject) -> BlogOutput:
        prompt = f"""
{BLOG_SYSTEM_PROMPT}

Content brief:
- Topic: {project.topic}
- Audience: {project.audience}
- Objective: {project.objective}
- Language: {project.language}

This is a draft for human review. Do not claim that it has been published.
""".strip()
        response = self._responder(prompt)
        return BlogOutput.model_validate(_extract_json_object(response))
