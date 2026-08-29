# Content Studio MVP

Content Studio keeps blog work independent from the existing video pipeline. A
content project can create and retain a research-backed blog draft even when no
video task exists.

## Evidence workflow

The research step accepts up to 10 user-supplied public HTTP/HTTPS URLs or can
discover candidates through Brave Search when `BRAVE_SEARCH_API_KEY` is configured.
Search snippets are not treated as evidence. The server removes duplicate and
unsafe candidate URLs, then applies the same process to every selected URL:

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

The Brave key is read only from the server environment and is never stored in a
project, SQLite payload, Streamlit state, or release bundle.

## Independent channel fan-out

After EvidencePack approval, one request can select `blog`, `short_video`, or both.
The two generators run independently from a deep copy of the same project snapshot:

```text
Approved EvidencePack
  +-- BlogGenerator -> BlogOutput
  +-- VideoPlanGenerator -> MoneyPrinterVideoAdapter -> existing MPT task queue
```

Each generated output lists the evidence claim IDs it used. A failed blog channel
does not cancel or delete the video task, and a failed video plan or task does not
delete the blog draft. The SQLite project snapshot stores separate channel status,
error, start/finish time, and LLM usage records.

The current MoneyPrinterTurbo LLM adapter returns text but not provider token or
pricing metadata. Content Studio therefore records one request and marks token/cost
measurement as `unavailable`; it does not fabricate dollar amounts.

The video adapter supplies both narration and material search terms to MPT, so the
MPT render does not make another script/term LLM request. It passes
`allow_cross_post=False`, ensuring a Content Studio render never inherits automatic
YouTube, TikTok, or Instagram upload settings. Rendering may still call configured
TTS and material-provider APIs and can incur their normal costs.

The `short` profile keeps a compact 1-20 scene plan. The `long` profile requests a
structured 6-12 minute, 8-30 scene plan, allows longer narration, and asks MPT to
process six paragraph units. Both profiles force `allow_cross_post=False`.

When both drafts exist, a deterministic consistency report compares used evidence
claim IDs and flags numbers that do not occur in any approved claim. This is a
review aid, not a replacement for human fact checking.

## Manual review and approval

The Review & Approve screen summarizes selected channel readiness and the quality
report. A reviewer can either approve the current outputs or request changes with a
required note. Cross-channel quality warnings require an explicit acknowledgement
before approval.

An approval stores:

- selected channels;
- review time and optional note;
- whether quality warnings were acknowledged; and
- a SHA-256 digest of the approved EvidencePack, channel outputs, and consistency
  report.

Regenerating a selected channel invalidates the approval and retains the prior
review record with an invalidation time and reason. Ghost synchronization recomputes
the snapshot digest and refuses to proceed if the current output no longer matches
the approval. This prevents an approval for one draft from being reused after the
draft changes.

Approval itself has no external side effect. An additional, explicit Ghost action
is required, and it still creates or updates only a `draft`. Approved videos remain
local; no external video publisher is connected in this stage.

## Release planning and local export

After approval, a reviewer can create a release plan for any channel included in
the current approval. Content Studio rechecks the approval digest before writing a
package under `storage/content/releases/<project-id>/<release-id>/` and a matching
ZIP archive. The bundle contains:

- the approved EvidencePack, review record, and consistency report;
- Markdown, HTML, and metadata for a selected blog output;
- the selected short-video plan, with rendered-file paths reduced to filenames;
- a release handoff record; and
- a manifest containing byte sizes and SHA-256 digests for every payload file.

The package deliberately does not copy rendered video media and does not call
Ghost or any video publisher. `planned_for` is time-zone-aware coordination
metadata, not an executable schedule. Every release record states
`external_actions_performed: false`. Regeneration, output changes, or a reviewer
requesting changes marks existing ready plans as `stale` while preserving their
immutable local artifacts for audit and comparison.

## Publication receipts and observations

A `ready` release can be linked to a URL only after the user confirms that the
content was already published outside Content Studio. The publication receipt
stores the release and approval digests, channel, platform label, public URL,
actual publication time, and an explicit
`external_action_performed_by_studio: false` marker.

The server performs a read-only URL check when a receipt or new observation is
recorded. It validates the initial URL and every redirect before making the next
request, rejecting credentials, localhost, private, link-local, and other
non-public destinations. Reachability, final URL, HTTP status, content type, and
response time are stored without downloading the response body.

Optional views, likes, comments, shares, and clicks are stored as `manual`
measurements in an append-only observation history. No provider account, private
analytics page, or publishing API is accessed. A stale release cannot receive a
new publication receipt, while existing receipts remain as historical evidence if
their source release later becomes stale.

REST endpoints:

```text
POST /api/v1/content/projects
POST /api/v1/content/projects/{project_id}/research
POST /api/v1/content/projects/{project_id}/discover
POST /api/v1/content/projects/{project_id}/evidence/approve
POST /api/v1/content/projects/{project_id}/fanout
POST /api/v1/content/projects/{project_id}/video/refresh
POST /api/v1/content/projects/{project_id}/review
POST /api/v1/content/projects/{project_id}/release-plans
POST /api/v1/content/projects/{project_id}/publications
POST /api/v1/content/projects/{project_id}/publications/{receipt_id}/observations
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
is synchronized. Local release directories and ZIP archives are stored below
`storage/content/releases/`; this ignored runtime directory is not committed.
