import os

from fastapi import Depends, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.content import (
    ContentFanoutRequest,
    ContentProjectCreate,
    ContentProjectListResponse,
    ContentProjectResponse,
    ContentReleaseRequest,
    ContentReviewRequest,
    PublicationObservationRequest,
    PublicationReceiptRequest,
)
from app.models.evidence import (
    EvidenceApprovalRequest,
    ResearchRequest,
    SourceDiscoveryRequest,
)
from app.models.exception import HttpException
from app.services.content import ContentWorkflow
from app.services.content.store import ContentProjectNotFoundError
from app.services.publishers import GhostPublisher, GhostPublisherConfig
from app.utils import utils


router = new_router(dependencies=[Depends(base.verify_token)])
_workflow: ContentWorkflow | None = None


def get_workflow() -> ContentWorkflow:
    global _workflow
    if _workflow is None:
        _workflow = ContentWorkflow()
    return _workflow


def _not_found(request: Request, project_id: str) -> HttpException:
    return HttpException(
        task_id=base.get_task_id(request),
        status_code=404,
        message=f"content project not found: {project_id}",
    )


@router.post(
    "/content/projects",
    response_model=ContentProjectResponse,
    summary="Create a content project",
)
def create_content_project(request: Request, body: ContentProjectCreate):
    project = get_workflow().create_project(body)
    return utils.get_response(200, project.model_dump(mode="json"))

@router.get(
    "/content/projects",
    response_model=ContentProjectListResponse,
    summary="List content projects",
)
def list_content_projects(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    projects = get_workflow().store.list(limit=limit, offset=offset)
    return utils.get_response(
        200, {"projects": [project.model_dump(mode="json") for project in projects]}
    )


@router.get(
    "/content/projects/{project_id}",
    response_model=ContentProjectResponse,
    summary="Get a content project",
)
def get_content_project(request: Request, project_id: str):
    try:
        project = get_workflow().store.get(project_id)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/research",
    response_model=ContentProjectResponse,
    summary="Verify sources and build an EvidencePack",
)
def research_content_project(
    request: Request, project_id: str, body: ResearchRequest
):
    try:
        project = get_workflow().research_evidence(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/discover",
    response_model=ContentProjectResponse,
    summary="Discover public sources with Brave Search and build an EvidencePack",
)
def discover_content_project(
    request: Request, project_id: str, body: SourceDiscoveryRequest
):
    try:
        project = get_workflow().discover_evidence(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/evidence/approve",
    response_model=ContentProjectResponse,
    summary="Approve a reviewed EvidencePack",
)
def approve_content_evidence(
    request: Request, project_id: str, body: EvidenceApprovalRequest
):
    try:
        project = get_workflow().approve_evidence(project_id, body.note)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/fanout",
    response_model=ContentProjectResponse,
    summary="Run independent blog and short-video channels",
)
def run_content_fanout(
    request: Request, project_id: str, body: ContentFanoutRequest
):
    try:
        project = get_workflow().run_fanout(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/video/refresh",
    response_model=ContentProjectResponse,
    summary="Refresh the MoneyPrinterTurbo video task state",
)
def refresh_content_video(request: Request, project_id: str):
    try:
        project = get_workflow().refresh_video(project_id)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/review",
    response_model=ContentProjectResponse,
    summary="Approve outputs or request changes after manual review",
)
def review_content_project(
    request: Request, project_id: str, body: ContentReviewRequest
):
    try:
        project = get_workflow().review_project(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/release-plans",
    response_model=ContentProjectResponse,
    summary="Create a local release plan and export bundle",
)
def create_content_release_plan(
    request: Request, project_id: str, body: ContentReleaseRequest
):
    try:
        project = get_workflow().create_release_plan(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/publications",
    response_model=ContentProjectResponse,
    summary="Record and verify a manually published URL",
)
def record_content_publication(
    request: Request, project_id: str, body: PublicationReceiptRequest
):
    try:
        project = get_workflow().record_publication(project_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/publications/{receipt_id}/observations",
    response_model=ContentProjectResponse,
    summary="Verify a publication URL and append manual performance metrics",
)
def observe_content_publication(
    request: Request,
    project_id: str,
    receipt_id: str,
    body: PublicationObservationRequest,
):
    try:
        project = get_workflow().observe_publication(project_id, receipt_id, body)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/blog",
    response_model=ContentProjectResponse,
    summary="Generate a blog draft for a content project",
)
def generate_blog_draft(request: Request, project_id: str):
    try:
        project = get_workflow().generate_blog(project_id)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))


@router.post(
    "/content/projects/{project_id}/ghost-draft",
    response_model=ContentProjectResponse,
    summary="Create or update a Ghost draft",
)
def sync_ghost_draft(request: Request, project_id: str):
    admin_url = os.getenv("GHOST_ADMIN_URL", "").strip()
    admin_api_key = os.getenv("GHOST_ADMIN_API_KEY", "").strip()
    if not admin_url or not admin_api_key:
        raise HttpException(
            task_id=base.get_task_id(request),
            status_code=503,
            message=(
                "Ghost draft publishing is not configured; set GHOST_ADMIN_URL "
                "and GHOST_ADMIN_API_KEY on the server"
            ),
        )
    try:
        publisher = GhostPublisher(
            GhostPublisherConfig(
                admin_url=admin_url,
                admin_api_key=admin_api_key,
                api_version=os.getenv("GHOST_ADMIN_API_VERSION", "v6.0"),
            )
        )
        project = get_workflow().sync_ghost_draft(project_id, publisher)
    except ContentProjectNotFoundError as exc:
        raise _not_found(request, project_id) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from exc
    return utils.get_response(200, project.model_dump(mode="json"))
