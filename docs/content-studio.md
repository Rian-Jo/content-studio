# Content Studio MVP

Content Studio keeps blog work independent from the existing video pipeline. A
content project can create and retain a blog draft even when no video task exists.

## Run locally

Start the independent Content Studio screen and create a project:

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
remain in the local project snapshot even after a Ghost draft is synchronized.
