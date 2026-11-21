# MACH Catalog API — Render deploy

Combined REST + MCP server for **MACH — Machine-Actionable Catalog for Healthcare**.
The DuckDB catalogue (206 entries, with embeddings) is baked into the image, so
the service serves immediately on wake — no boot-time harvest.

## Deploy to Render

1. Push this folder to a GitHub repo (see below).
2. On render.com → **New → Web Service** → connect that repo.
3. Render auto-detects `render.yaml` (free plan, Docker). Create the service.
4. It builds the image and goes live at `https://mach-catalog.onrender.com`
   (or whatever name Render assigns).

The free plan sleeps after ~15 min idle; the next request wakes it (~50s).

## Endpoints

- `GET /catalog` — list entries (`?category=software|ai-ml-models|...`)
- `GET /dataset/{id}` — one entry: seed + harvested facts, each with its source
- `POST /search` — semantic search `{"query": "...", "limit": 5}`
- `GET /livez` / `GET /readyz` — health / readiness
- `GET /stats` — MCP usage pulse: per-tool call counts + last-called time. A
  privacy-safe tally (no caller identity, no inputs, only counts). Counts are
  "since" the timestamp in the response — they restart on redeploy unless
  `MACH_ANALYTICS_PATH` points at durable storage (see below).
- `GET /docs` — OpenAPI UI
- `/mcp` — MCP server (stateless HTTP) for AI agents. Tools: `list_entries`,
  `get_entry`, `get_history`, `search_catalog`, `explain_scoring`.

### Environment

- `MACH_DB_PATH` — DuckDB snapshot to serve (default `data/mach.duckdb`).
- `MACH_CORS_ORIGINS` — comma-separated allowed origins, or `*` for any.
- `MACH_ANALYTICS_PATH` — optional file for the `/stats` tally. When set, counts
  are loaded on startup and written back after each MCP call (atomic, best-effort),
  so they survive within a container's lifetime. Unset, or on an ephemeral
  filesystem (e.g. Render's free tier), counts are in-memory and reset on redeploy.

Connect an MCP client (Claude Desktop, Cursor):

```json
{ "mcpServers": { "mach": { "url": "https://<your-service>.onrender.com/mcp" } } }
```

## Refreshing the catalogue

The DB is a baked snapshot. To update it: rebuild locally
(`uv run python ingest.py --db data/mach.duckdb` with a GITHUB_TOKEN), copy the
new `data/mach.duckdb` here, commit, and push — Render redeploys.

<!-- private-repo deploy test 20261108 -->
