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
    st.write(f"블로그: `{project.blog_status.value}`")
    st.write(f"Ghost: `{project.ghost_status.value}`")
    st.write(f"검토: `{project.approval_status.value}`")
    if st.button("블로그 초안 생성", type="primary", use_container_width=True):
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
    st.subheader("블로그 미리보기")
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
        st.info("초안을 생성하면 여기에 미리보기가 표시됩니다.")
