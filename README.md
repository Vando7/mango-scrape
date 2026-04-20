# deep-dive-mcp

Two pieces:

1. **Scraper service** (Docker) — [Dockerfile](Dockerfile), [server.py](server.py), [scraper.py](scraper.py). FastAPI + patchright + trafilatura. Listens on `localhost:8765`.
2. **MCP shim** (spawned by LM Studio) — [shim/mcp_shim.py](shim/mcp_shim.py). Forwards `deep_dive` tool calls to the container over HTTP.

```
LM Studio ──stdio──▶ mcp_shim.py ──HTTP──▶ localhost:8765 ──▶ FastAPI ──▶ patchright ──▶ trafilatura
```

## Run the scraper

```bash
docker compose up -d --build

# Sanity checks
curl -s http://localhost:8765/health
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' -d '{"urls":["https://example.com"]}' | jq .
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

Override the service URL (default `http://localhost:8765`) with `"DEEP_DIVE_URL"` in `env`.

## Troubleshooting

- **"scraper service unreachable"** → container isn't running. `docker compose ps`, restart if needed.
- **Shim won't start** → usually `mcp`/`httpx` missing, or `command` path in mcp.json wrong (LM Studio doesn't inherit PATH — use absolute paths).
- **Bot-protected site still blocks** → patchright handles most CDP fingerprinting, not IP reputation or advanced challenges. Next step would be a proxy or swapping to camoufox for that site.
