# deep-dive-mcp

Two pieces:

1. **Scraper service** — [server.py](server.py), [scraper.py](scraper.py). FastAPI + patchright + trafilatura. Listens on `localhost:8765`.
2. **MCP shim** (spawned by LM Studio) — [shim/mcp_shim.py](shim/mcp_shim.py). Converts all tool responses to flat key=value strings (no JSON, no brackets). Auto-starts the scraper on first call; if the scraper has idle-shut down (5 min default), the next tool call transparently respawns it.

```
LM Studio ──stdio──▶ mcp_shim.py ──HTTP──▶ localhost:8765 ──▶ FastAPI ──▶ patchright ──▶ trafilatura
```

Docker is also supported (see [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml)) but the auto-start path is lighter on system resources and is the primary mode. Under docker-compose the container listens on `8765` internally and is mapped to `9161` on the host. Override the shim's target with `DEEP_DIVE_URL` (e.g. `http://localhost:9161`) — non-local hosts disable the auto-start fallback.

## Run the scraper

The shim does this for you on first tool call. To run manually:

```bash
uv sync                              # installs deps + managed Python into .venv
uv run patchright install chromium   # patched Chromium binary (one-time)
uv run uvicorn server:app --port 8765

# Sanity checks
curl -s http://localhost:8765/health
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' -d '{"urls":["https://example.com"]}' | jq .
```

Or run via Docker:

```bash
docker compose up -d --build
curl -s http://localhost:9161/health
# Logs: docker compose logs -f scraper
# Stop: docker compose down
```

The server exits after `DEEP_DIVE_IDLE_TIMEOUT` seconds (default 300) of no requests. Set to `0` to disable.

## Install the shim

Copy `shim/` to the machine running LM Studio. Install its deps:

```bash
# uv (recommended)
uv sync --project /path/to/shim

# or plain pip
pip install mcp httpx
```

## Wire into LM Studio

Edit `~/.lmstudio/mcp.json` (Windows: `%USERPROFILE%\.lmstudio\mcp.json`):

```json
{
  "mcpServers": {
    "deep-dive": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--project", "/path/to/shim", "python", "/path/to/shim/mcp_shim.py"],
      "env": { "PYTHONUNBUFFERED": "1" }
    }
  }
}
```

Or with plain Python:

```json
{
  "mcpServers": {
    "deep-dive": {
      "command": "/absolute/path/to/python",
      "args": ["/path/to/shim/mcp_shim.py"],
      "env": { "PYTHONUNBUFFERED": "1" }
    }
  }
}
```

Save — LM Studio auto-reloads. Shim stderr shows in the Program tab; scraper tracebacks show in `docker compose logs`.

## Available tools

All text-based tools return flat key=value lines (no JSON). Multi-line values keep real newlines — no escape artifacts. Results for multiple URLs are separated by `===`.

- `deep_dive(urls, timeout_s)` — fetches URLs and returns clean markdown + metadata
- `deep_dive_screenshot(urls, timeout_s)` — takes full-page screenshots; on success returns ImageContent blocks the model can see, on failure returns TextContent with the error
- `get_youtube_transcript(urls, timeout_s)` — YouTube transcript + video metadata (paginated)
- `deep_dive_transcript_page(video_id, page_num)` — fetches a specific page of a paginated transcript
- `web_search(query, num_results, language, deep)` — Brave search; `deep=True` also scrapes every result and merges the markdown into the same block (use sparingly — high token cost)
- `hn_search(query, num_results, tags, sort_by_date)` — Hacker News via Algolia (free, no auth). Tags: `story`, `comment`, `front_page`, `ask_hn`, `show_hn`, `author_<name>`
- `reddit_search(query, num_results, subreddit, sort, time)` — Reddit public JSON. Rate-limited; scope to a subreddit if hit with 429/403
- `download_file(url)` — downloads a file into workspace (capped at `DEEP_DIVE_MAX_DOWNLOAD_MB`, default 100)
- `clone_repo(git_url)` — clones a git repo into workspace
- `list_files(path)` — lists files in workspace
- `cat_file(path)` — reads a file from workspace
- `get_date()` — today's date (handy for the model)

### Reddit support

Reddit URLs are handled automatically with a fallback chain: www.reddit.com → old.reddit.com → m.reddit.com. The scraper scrolls to trigger lazy-loaded comments and waits longer for JS-rendered content. Typical results:
- **www.reddit.com**: ~500+ words, 30-40 top comments captured
- **m.reddit.com**: simpler HTML, fewer comments but more reliable
- **old.reddit.com**: cleanest HTML structure but often blocked by bot detection

Override the service URL (default `http://localhost:9161`) with `"DEEP_DIVE_URL"` in `env`.

## Output format

All text-based tool responses are flat key=value lines:
```
url=https://example.com
status=ok
title=Page Title
description=A short description
that spans real lines
markdown_content=First paragraph

Second paragraph

Third paragraph
word_count=150
links=About | https://example.com/about
Contact | https://example.com/contact
===
url=https://another.com
status=error
error=navigation failed: TimeoutError
```
No JSON, no brackets, no indentation. Multi-line values keep real newlines (the value continues on subsequent lines until the next `key=` or the `===` divider).

## Troubleshooting

- **"service unreachable" persists across calls** → the shim's auto-start failed. Run uvicorn manually (see [Run the scraper](#run-the-scraper)) and check stderr. Common cause: missing `.venv` or `patchright install chromium` not run.
- **Shim won't start** → usually `mcp`/`httpx` missing, or `command` path in mcp.json wrong (LM Studio doesn't inherit PATH — use absolute paths).
- **Bot-protected site still blocks** → patchright handles most CDP fingerprinting, not IP reputation or advanced challenges. Next step would be a proxy or swapping to camoufox for that site.
- **Reddit returns few comments via `deep_dive`** → `www.reddit.com` loads ~30-40 top comments via JS lazy-loading. `old.reddit.com` has cleaner HTML but is often blocked by bot detection. `m.reddit.com` works reliably but shows fewer comments.
- **`reddit_search` returns 429/403** → Reddit rate-limits unauthenticated calls. Scope the query to a specific subreddit (`subreddit="python"`) or back off for a minute.
- **`hn_search` is empty** → Algolia is reachable from most networks; if your VPN/firewall blocks `hn.algolia.com`, the call fails. Try without VPN.
