# deep-dive-mcp

Two pieces:

1. **Scraper service** (Docker) — [Dockerfile](Dockerfile), [server.py](server.py), [scraper.py](scraper.py). FastAPI + patchright + trafilatura. Listens on `localhost:8765`. Returns compact JSON.
2. **MCP shim** (spawned by LM Studio) — [shim/mcp_shim.py](shim/mcp_shim.py). Forwards `deep_dive` and `deep_dive_screenshot` tool calls to the container over HTTP.

```
LM Studio ──stdio──▶ mcp_shim.py ──HTTP──▶ localhost:8765 ──▶ FastAPI ──▶ patchright ──▶ trafilatura
```

## Run the scraper

```bash
docker compose up -d --build

# Sanity checks
curl -s http://localhost:8765/health
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' -d '{"urls":["https://example.com"]}' | jq .
curl -s -X POST http://localhost:8765/screenshot -H 'Content-Type: application/json' -d '{"urls":["https://example.com"]}' | jq '.[0].screenshot_b64 | length'

# Reddit (auto-scrolls for comments)
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' -d '{"urls":["https://www.reddit.com/r/LocalLLaMA/comments/1ssl1xh/qwen_36_27b_is_out/"]}' | jq '.[0].word_count'
```

Logs: `docker compose logs -f scraper`. Stop: `docker compose down`.

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

- `deep_dive(urls, timeout_s)` — fetches URLs and returns clean markdown + metadata
- `deep_dive_screenshot(urls, timeout_s)` — takes full-page screenshots, returned as images the model can see

### Reddit support

Reddit URLs are handled automatically with a fallback chain: www.reddit.com → old.reddit.com → m.reddit.com. The scraper scrolls to trigger lazy-loaded comments and waits longer for JS-rendered content. Typical results:
- **www.reddit.com**: ~500+ words, 30-40 top comments captured
- **m.reddit.com**: simpler HTML, fewer comments but more reliable
- **old.reddit.com**: cleanest HTML structure but often blocked by bot detection

Override the service URL (default `http://localhost:8765`) with `"DEEP_DIVE_URL"` in `env`.

## Troubleshooting

- **"scraper service unreachable"** → container isn't running. `docker compose ps`, restart if needed.
- **Shim won't start** → usually `mcp`/`httpx` missing, or `command` path in mcp.json wrong (LM Studio doesn't inherit PATH — use absolute paths).
- **Bot-protected site still blocks** → patchright handles most CDP fingerprinting, not IP reputation or advanced challenges. Next step would be a proxy or swapping to camoufox for that site.
- **Reddit returns few comments** → www.reddit.com loads ~30-40 top comments via JS lazy-loading. old.reddit.com has cleaner HTML but is often blocked by bot detection. m.reddit.com works reliably but shows fewer comments.
