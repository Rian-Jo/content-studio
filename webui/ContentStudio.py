"""Independent Streamlit entry point for the Content Studio MVP."""

import os
import sys
from pathlib import Path

import streamlit as st


root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) in sys.path:
    sys.path.remove(str(root_dir))
sys.path.insert(0, str(root_dir))

from app.config import config  # noqa: E402
from app.models.content import (  # noqa: E402
    ApprovalStatus,
    ContentChannel,
    ContentFanoutRequest,
    ContentProjectCreate,
    ContentReviewRequest,
    ReviewDecision,
    VideoGenerationOptions,
    VideoStatus,
)
from app.models.evidence import (  # noqa: E402
    EvidenceStatus,
    ResearchRequest,
    SourceInput,
)
from app.services.content import ContentWorkflow  # noqa: E402
from app.services.publishers import (  # noqa: E402
    GhostPublisher,
    GhostPublisherConfig,
)


st.set_page_config(page_title="Content Studio", page_icon="📝", layout="wide")
st.title("Content Studio")
st.caption("통합된 제품, 분리된 파이프라인 — 블로그 초안은 영상 작업과 독립적으로 생성됩니다.")


@st.cache_resource
def workflow() -> ContentWorkflow:
    return ContentWorkflow()


with st.form("new-content-project"):
    topic = st.text_input("주제", placeholder="예: Ghost 자동 발행 시스템 구현")
    audience = st.text_input("대상 독자", value="기술 자동화에 관심 있는 실무자")
    objective = st.text_input("콘텐츠 목적", value="실용적인 단계별 가이드 제공")
    language = st.selectbox("언어", ["ko", "en", "auto"])
    create = st.form_submit_button("프로젝트 만들기", type="primary")

if create:
    if not topic.strip():
        st.error("주제를 입력해 주세요.")
    else:
        project = workflow().create_project(
            ContentProjectCreate(
                topic=topic.strip(),
                audience=audience.strip(),
                objective=objective.strip(),
                language=language,
            )
        )
        st.session_state["content_project_id"] = project.project_id
        st.success("콘텐츠 프로젝트를 만들었습니다.")

projects = workflow().store.list(limit=100)
if not projects:
    st.info("아직 콘텐츠 프로젝트가 없습니다.")
    st.stop()

labels = {project.project_id: f"{project.topic} · {project.blog_status.value}" for project in projects}
default_id = st.session_state.get("content_project_id", projects[0].project_id)
project_ids = list(labels)
selected_id = st.selectbox(
    "프로젝트",
    project_ids,
    index=project_ids.index(default_id) if default_id in project_ids else 0,
    format_func=lambda value: labels[value],
)
project = workflow().store.get(selected_id)

left, right = st.columns([1, 2])
with left:
    st.subheader("상태")
    st.write(f"근거: `{project.evidence_status.value}`")
    st.write(f"블로그: `{project.blog_status.value}`")
    st.write(f"영상: `{project.video_status.value}`")
    st.write(f"Ghost: `{project.ghost_status.value}`")
    st.write(f"검토: `{project.approval_status.value}`")
    has_channel_output = (
        project.blog_output is not None or project.video_output is not None
    )
    with st.form("research-sources"):
        source_urls = st.text_area(
            "검증할 출처 URL",
            placeholder="https://example.com/source-1\nhttps://example.com/source-2",
            help="공개 HTTP/HTTPS 문서만 지원하며, 한 줄에 URL 하나씩 최대 10개입니다.",
            disabled=has_channel_output,
        )
        research = st.form_submit_button(
            "자료 조사 및 EvidencePack 만들기",
            use_container_width=True,
            disabled=has_channel_output,
        )
    if research:
        urls = [line.strip() for line in source_urls.splitlines() if line.strip()]
        try:
            research_request = ResearchRequest(
                sources=[SourceInput(url=url) for url in urls]
            )
            with st.spinner("출처를 검증하고 EvidencePack을 만들고 있습니다..."):
                workflow().research_evidence(project.project_id, research_request)
        except Exception as exc:
            st.error(f"EvidencePack 생성 실패: {exc}")
        else:
            st.success("검토할 EvidencePack을 만들었습니다.")
            st.rerun()

    evidence_ready = project.evidence_status == EvidenceStatus.ready_for_review
    confirm_evidence = st.checkbox(
        "출처와 주장 연결을 직접 검토했습니다.",
        disabled=not evidence_ready,
    )
    if st.button(
        "EvidencePack 승인",
        disabled=not evidence_ready or not confirm_evidence,
        use_container_width=True,
    ):
        try:
            workflow().approve_evidence(project.project_id)
        except Exception as exc:
            st.error(f"EvidencePack 승인 실패: {exc}")
        else:
            st.success("EvidencePack을 승인했습니다.")
            st.rerun()

    configured_source = config.app.get("video_source", "pexels")
    safe_source = (
        configured_source
        if configured_source in {"pexels", "pixabay", "coverr"}
        else "pexels"
    )
    with st.form("channel-fanout"):
        selected_channels = st.multiselect(
            "생성 채널",
            options=["blog", "short_video"],
            default=["blog"],
            format_func=lambda value: {
                "blog": "블로그",
                "short_video": "숏폼 영상",
            }[value],
            disabled=project.evidence_status != EvidenceStatus.approved,
        )
        video_selected = "short_video" in selected_channels
        video_source = st.selectbox(
            "영상 소재 공급자",
            ["pexels", "pixabay", "coverr"],
            index=["pexels", "pixabay", "coverr"].index(safe_source),
            disabled=not video_selected,
        )
        video_aspect = st.selectbox(
            "영상 비율",
            ["9:16", "16:9", "1:1"],
            disabled=not video_selected,
        )
        voice_name = st.text_input(
            "MPT 음성 ID",
            value=str(config.ui.get("voice_name", "") or ""),
            help="기존 MoneyPrinterTurbo에서 검증한 음성 ID를 사용하세요.",
            disabled=not video_selected,
        )
        confirm_video_cost = st.checkbox(
            "영상 생성 시 설정된 LLM·TTS·소재 API 비용이 발생할 수 있음을 확인했습니다.",
            disabled=not video_selected,
        )
        regenerate = st.checkbox(
            "선택 채널의 기존 결과를 재생성",
            value=project.approval_status == ApprovalStatus.changes_requested,
            help="재생성하면 기존 결과에 대한 승인이 자동으로 무효화됩니다.",
        )
        fanout = st.form_submit_button(
            "선택 채널 생성",
            type="primary",
            use_container_width=True,
            disabled=(
                project.evidence_status != EvidenceStatus.approved
                or not selected_channels
                or (
                    video_selected
                    and (not confirm_video_cost or not voice_name.strip())
                )
            ),
        )
    if fanout:
        request = ContentFanoutRequest(
            channels=selected_channels,
            video_options=VideoGenerationOptions(
                video_source=video_source,
                video_aspect=video_aspect,
                voice_name=voice_name.strip(),
            ),
            regenerate=regenerate,
        )
        with st.spinner("선택한 채널을 독립적으로 실행하고 있습니다..."):
            try:
                workflow().run_fanout(project.project_id, request)
            except Exception as exc:
                st.error(f"채널 생성 시작 실패: {exc}")
            else:
                st.success("채널 작업 결과를 저장했습니다.")
                st.rerun()

    if project.video_status in {VideoStatus.queued, VideoStatus.rendering}:
        if st.button("영상 작업 상태 새로고침", use_container_width=True):
            try:
                workflow().refresh_video(project.project_id)
            except Exception as exc:
                st.error(f"영상 상태 확인 실패: {exc}")
            else:
                st.rerun()

    if project.channel_runs:
        with st.expander("채널 실행·LLM 비용 기록"):
            st.json(
                {
                    channel: run.model_dump(mode="json")
                    for channel, run in project.channel_runs.items()
                }
            )

    ghost_ready = bool(os.getenv("GHOST_ADMIN_URL") and os.getenv("GHOST_ADMIN_API_KEY"))
    current_approval = workflow().review_service.is_current_approval(project)
    blog_approved = bool(
        current_approval
        and project.review_record
        and ContentChannel.blog in project.review_record.reviewed_channels
    )
    confirm_ghost = st.checkbox(
        "승인된 블로그를 Ghost 외부 초안으로 생성/갱신함을 확인했습니다.",
        disabled=not ghost_ready or not blog_approved,
    )
    if st.button(
        "Ghost 초안으로 보내기",
        disabled=not confirm_ghost,
        use_container_width=True,
    ):
        publisher = GhostPublisher(
            GhostPublisherConfig(
                admin_url=os.environ["GHOST_ADMIN_URL"],
                admin_api_key=os.environ["GHOST_ADMIN_API_KEY"],
                api_version=os.getenv("GHOST_ADMIN_API_VERSION", "v6.0"),
            )
        )
        with st.spinner("Ghost 초안을 동기화하고 있습니다..."):
            try:
                workflow().sync_ghost_draft(project.project_id, publisher)
            except Exception as exc:
                st.error(f"Ghost 동기화 실패: {exc}")
            else:
                st.success("Ghost 초안을 동기화했습니다. 공개 발행은 하지 않았습니다.")
                st.rerun()
    if not ghost_ready:
        st.caption("Ghost 연동은 서버 환경변수 설정 후 활성화됩니다.")
    elif not blog_approved:
        st.caption("현재 결과 스냅샷을 승인한 후 Ghost 초안을 동기화할 수 있습니다.")

with right:
    evidence_tab, blog_tab, video_tab, quality_tab, review_tab = st.tabs(
        [
            "Research & Evidence",
            "블로그 미리보기",
            "영상",
            "일관성 검사",
            "Review & Approve",
        ]
    )
    with evidence_tab:
        if project.evidence_pack:
            st.subheader("검증된 출처")
            for source in project.evidence_pack.sources:
                label = source.title or source.publisher or source.requested_url
                st.text(f"{label} — {source.verification_status.value}")
                st.caption(source.final_url or source.requested_url)
                if source.error:
                    st.caption(source.error)

            st.subheader("핵심 주장과 출처 연결")
            source_labels = {
                source.source_id: source.title
                or source.publisher
                or source.requested_url
                for source in project.evidence_pack.sources
            }
            for claim in project.evidence_pack.claims:
                labels_for_claim = [
                    source_labels[source_id] for source_id in claim.source_ids
                ]
                st.markdown(f"- {claim.statement}")
                st.caption(
                    f"신뢰도: {claim.confidence} · 출처: {', '.join(labels_for_claim)}"
                )
            with st.expander("메시지·반론·SEO 키워드"):
                st.json(
                    {
                        "key_messages": project.evidence_pack.key_messages,
                        "counterpoints": project.evidence_pack.counterpoints,
                        "seo_keywords": project.evidence_pack.seo_keywords,
                    }
                )
        else:
            st.info("출처 URL을 입력하면 검증 결과와 주장 연결이 표시됩니다.")

    with blog_tab:
        if project.blog_output:
            st.markdown(project.blog_output.markdown)
            with st.expander("SEO 메타데이터"):
                st.json(
                    {
                        "slug": project.blog_output.slug,
                        "seo_title": project.blog_output.seo_title,
                        "meta_description": project.blog_output.meta_description,
                        "tags": project.blog_output.tags,
                    }
                )
        else:
            st.info("승인된 EvidencePack으로 초안을 생성하면 표시됩니다.")

    with video_tab:
        if project.video_output:
            st.subheader(project.video_output.title)
            st.write(project.video_output.hook)
            st.caption(f"MPT task: {project.video_output.task_id}")
            st.write(project.video_output.narration)
            with st.expander("장면·검색어·게시문 초안"):
                st.json(
                    {
                        "scenes": [
                            scene.model_dump(mode="json")
                            for scene in project.video_output.scenes
                        ],
                        "search_terms": project.video_output.search_terms,
                        "caption": project.video_output.caption,
                        "hashtags": project.video_output.hashtags,
                        "evidence_claim_ids": (
                            project.video_output.evidence_claim_ids
                        ),
                    }
                )
            for rendered_file in project.video_output.rendered_files:
                if Path(rendered_file).is_file():
                    st.video(rendered_file)
        else:
            st.info("숏폼 영상 채널을 선택하면 영상 계획과 MPT 작업이 표시됩니다.")

    with quality_tab:
        report = project.consistency_report
        st.write(f"상태: `{report.status.value}`")
        if report.issues:
            for issue in report.issues:
                st.warning(issue)
        elif report.status.value == "passed":
            st.success("승인된 주장 밖의 수치가 발견되지 않았습니다.")
        else:
            st.info("블로그와 영상 결과가 모두 준비되면 검사가 실행됩니다.")
        st.json(report.model_dump(mode="json"))

    with review_tab:
        st.subheader("통합 검토")
        requested = set(project.requested_channels)
        readiness = {
            "blog": (
                "not_selected"
                if ContentChannel.blog not in requested
                else project.blog_status.value
            ),
            "short_video": (
                "not_selected"
                if ContentChannel.short_video not in requested
                else project.video_status.value
            ),
            "quality": project.consistency_report.status.value,
        }
        st.json(readiness)

        blog_review_ready = (
            ContentChannel.blog not in requested
            or project.blog_status.value == "draft_complete"
        )
        video_review_ready = (
            ContentChannel.short_video not in requested
            or project.video_status == VideoStatus.complete
        )
        has_reviewable_output = bool(
            (ContentChannel.blog in requested and project.blog_output)
            or (ContentChannel.short_video in requested and project.video_output)
        )
        quality_warning = project.consistency_report.status.value == "warning"

        with st.form("content-review"):
            review_note = st.text_area(
                "검토 메모",
                placeholder="승인 근거 또는 필요한 수정 사항을 기록하세요.",
            )
            acknowledge_warnings = st.checkbox(
                "일관성 검사 경고를 검토하고 승인에 반영했습니다.",
                disabled=not quality_warning,
            )
            approve, request_changes = st.columns(2)
            approve_clicked = approve.form_submit_button(
                "현재 결과 승인",
                type="primary",
                use_container_width=True,
                disabled=(
                    not has_reviewable_output
                    or not blog_review_ready
                    or not video_review_ready
                    or (quality_warning and not acknowledge_warnings)
                ),
            )
            changes_clicked = request_changes.form_submit_button(
                "수정 요청",
                use_container_width=True,
                disabled=not has_reviewable_output,
            )

        if approve_clicked or changes_clicked:
            decision = (
                ReviewDecision.approve
                if approve_clicked
                else ReviewDecision.request_changes
            )
            try:
                workflow().review_project(
                    project.project_id,
                    ContentReviewRequest(
                        decision=decision,
                        note=review_note,
                        acknowledge_quality_warnings=acknowledge_warnings,
                    ),
                )
            except Exception as exc:
                st.error(f"검토 저장 실패: {exc}")
            else:
                st.success("검토 결정을 저장했습니다.")
                st.rerun()

        if project.review_record:
            st.subheader("현재 검토 기록")
            st.json(project.review_record.model_dump(mode="json"))
        if (
            current_approval
            and ContentChannel.short_video in requested
            and project.video_status == VideoStatus.complete
        ):
            st.info(
                "영상은 승인된 로컬 결과입니다. 외부 영상 게시 기능은 아직 "
                "연결하지 않았으며 자동 업로드도 비활성화되어 있습니다."
            )
