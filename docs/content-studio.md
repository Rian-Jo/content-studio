# Content Studio MVP

Content Studio keeps blog work independent from the existing video pipeline. A
content project can create and retain a research-backed blog draft even when no
video task exists.

## Evidence workflow

The current research step accepts up to 10 user-supplied public HTTP/HTTPS URLs. It
does not discover sources automatically. For each URL, the server:

1. rejects credential-bearing, local, private, link-local, and other non-public
   targets, including redirect targets;
2. fetches a size-limited text, HTML, Markdown, or JSON response;
3. stores the final URL, title, publisher, HTTP status, content type, retrieval
   time, readable excerpt, and SHA-256 content digest;
4. asks the configured LLM to derive claims using only verified excerpts; and
5. validates that every claim references a verified `source_id`.

The resulting `EvidencePack` remains `ready_for_review` until a person approves it.
Blog generation is rejected before approval. Source excerpts are explicitly marked
as untrusted reference data in LLM prompts to reduce prompt-injection risk.

REST endpoints:

```text
POST /api/v1/content/projects
POST /api/v1/content/projects/{project_id}/research
POST /api/v1/content/projects/{project_id}/evidence/approve
POST /api/v1/content/projects/{project_id}/blog
POST /api/v1/content/projects/{project_id}/ghost-draft
GET  /api/v1/content/projects
GET  /api/v1/content/projects/{project_id}
```

## Run locally

Start the independent Content Studio screen, create a project, add source URLs,
review the generated claim-to-source links, and approve the EvidencePack:

```powershell
uv run streamlit run webui/ContentStudio.py
```

Keeping this entry point separate preserves MoneyPrinterTurbo's existing single-page
WebUI behavior. Blog generation uses the LLM provider already configured in
MoneyPrinterTurbo.

Ghost integration is optional and server-side only:

```text
GHOST_ADMIN_URL=https://your-admin-domain.example
GHOST_ADMIN_API_KEY=id:hex-secret
GHOST_ADMIN_API_VERSION=v6.0
```

The key is never stored in `config.toml`, content project JSON, SQLite payloads, or
the WebUI state. The current publisher only creates or updates a `draft`; it does
not expose a public-publish operation.

Content state is stored in `storage/content/content.db`. Generated Markdown and HTML
and the EvidencePack remain in the local project snapshot even after a Ghost draft
is synchronized.
