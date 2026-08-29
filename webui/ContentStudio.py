"""Independent Streamlit entry point for the Content Studio MVP."""

import os
import sys
from pathlib import Path

import streamlit as st


root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) in sys.path:
    sys.path.remove(str(root_dir))
sys.path.insert(0, str(root_dir))

from app.models.content import ContentProjectCreate  # noqa: E402
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
    st.write(f"Ghost: `{project.ghost_status.value}`")
    st.write(f"검토: `{project.approval_status.value}`")
    with st.form("research-sources"):
        source_urls = st.text_area(
            "검증할 출처 URL",
            placeholder="https://example.com/source-1\nhttps://example.com/source-2",
            help="공개 HTTP/HTTPS 문서만 지원하며, 한 줄에 URL 하나씩 최대 10개입니다.",
            disabled=project.blog_output is not None,
        )
        research = st.form_submit_button(
            "자료 조사 및 EvidencePack 만들기",
            use_container_width=True,
            disabled=project.blog_output is not None,
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

    blog_enabled = project.evidence_status == EvidenceStatus.approved
    if st.button(
        "블로그 초안 생성",
        type="primary",
        use_container_width=True,
        disabled=not blog_enabled,
    ):
        with st.spinner("블로그 초안을 생성하고 있습니다..."):
            try:
                workflow().generate_blog(project.project_id)
            except Exception as exc:
                st.error(f"초안 생성 실패: {exc}")
            else:
                st.success("블로그 초안을 생성했습니다.")
                st.rerun()

    ghost_ready = bool(os.getenv("GHOST_ADMIN_URL") and os.getenv("GHOST_ADMIN_API_KEY"))
    confirm_ghost = st.checkbox(
        "Ghost 외부 초안을 생성/갱신하는 작업임을 확인했습니다.",
        disabled=not ghost_ready or project.blog_output is None,
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

with right:
    evidence_tab, blog_tab = st.tabs(["Research & Evidence", "블로그 미리보기"])
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
