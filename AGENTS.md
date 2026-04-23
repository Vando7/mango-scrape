# AGENTS.md

Personal-use "deep dive" scraper for local LLMs. Takes URLs, returns clean markdown + metadata. Intended workflow: a local model gets search snippets (e.g. from SearXNG), then calls `deep_dive` on results it wants to read in full.

## Architecture

```
LM Studio
  │  spawns via stdio
  ▼
mcp_shim.py (thin) ──HTTP──▶ localhost:8765
                                  │
                                  ▼
                            Docker container
                                  │
                                  ▼
                            server.py (FastAPI)
                                  │
                                  ▼
                            scraper.py (patchright + trafilatura)
```

The container owns all heavy deps (patched Chromium, extraction). The shim is intentionally tiny: stdio MCP in, one HTTP POST out.

## Files

| Path | Role |
|---|---|
| `scraper.py` | Scraping logic. `launch_browser`, `_scroll_page`, `_extract_reddit_comments`, `scrape_one`, `scrape_many`. Reddit-specific: scroll + network idle wait + include_comments=True + www→old.reddit→m.reddit fallback chain. |
| `server.py` | FastAPI wrapper. Lifespan owns a shared browser. `POST /scrape`, `POST /screenshot`, `GET /health`. |
| `Dockerfile` | Based on `mcr.microsoft.com/playwright/python` so Chromium's system libs are pre-installed. |
| `docker-compose.yml` | Binds `127.0.0.1:8765`, sets `shm_size: 1gb`. |
| `shim/mcp_shim.py` | stdio MCP server. Forwards `deep_dive` and `deep_dive_screenshot` to the container. |
| `shim/pyproject.toml` | Shim deps: `mcp`, `httpx`. |
| `pyproject.toml` | Host-side dev deps (for running `server.py` without Docker). |
| `README.md` | Human-facing setup. |

## Commands

```bash
# Build + run
docker compose up -d --build

# Logs
docker compose logs -f scraper

# Smoke tests
curl -s http://localhost:8765/health
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"]}' | jq .

# Stop
docker compose down

# Local dev without Docker (requires uv + system libs for Chromium)
uv sync
uv run patchright install chromium
uv run uvicorn server:app --reload --port 8765
```

## Conventions and gotchas

**Stdio MCP hygiene (shim).** The shim speaks JSON-RPC over stdout. Never `print()` in `mcp_shim.py` or anything it imports. All logging to stderr (`logging.basicConfig(stream=sys.stderr, ...)`). A stray stdout byte corrupts the protocol and the client closes the connection silently.

**Use patchright, not playwright.** Import is `from patchright.async_api import …`. Patchright patches the `Runtime.enable` CDP leak that regular Playwright (and `playwright-stealth`) can't mask. Chromium-only. Do not "upgrade" to vanilla Playwright — you lose stealth.

**Patchright's Chromium is separate.** `pip install patchright` does not fetch a browser. `patchright install chromium` downloads a patched build distinct from any existing Playwright browser. Dockerfile handles this; local dev must run it once.

**Shared browser, fresh context per URL.** `server.py` launches one browser in `lifespan` and reuses it. Each scrape creates a new `browser.new_context()` (cheap) and closes it. Do not launch per request (1–2 s overhead). Do not reuse contexts (leaks cookies/state).

**Errors are per-URL, never raise.** `scrape_one` catches everything and returns `{status: "error", error: ...}`. `scrape_many` uses `asyncio.gather(..., return_exceptions=True)`. Callers always get one result per input URL — preserve this contract.

**Bind to localhost.** `docker-compose.yml` uses `"127.0.0.1:8765:8765"`. Don't change to `0.0.0.0` unless explicitly asked to serve other hosts.

**Chromium in Docker needs `shm_size`.** The default 64 MB `/dev/shm` crashes Chromium under real pages. Keep `shm_size: 1gb`. Patchright passes `--no-sandbox` and `--disable-dev-shm-usage` automatically.

**LM Studio mcp.json.** Located at `~/.lmstudio/mcp.json` (or `%USERPROFILE%\.lmstudio\mcp.json` on Windows). Follows Cursor's notation: `{"mcpServers": {...}}` with `command`/`args`/`env` for stdio, `{"url": ...}` for remote. LM Studio spawns processes without inheriting shell PATH — `command` must be an absolute path. Per-server stderr is visible in the Program tab.

**Screenshots return MCP ImageContent.** `deep_dive_screenshot` in the shim forwards screenshot b64 from the container into `mcp.types.ImageContent(type="image", data=<b64>, mimeType="image/png")` blocks. These serialize directly as image content blocks with base64 data and mime type — the model sees actual images, not opaque strings.

**Reddit scraping.** For `reddit.com` URLs (not old.reddit or m.reddit), scraper.py tries: www first → old.reddit → m.reddit. On www.reddit.com it waits longer for network idle (`timeout_s * 500ms`) and scrolls the page to trigger lazy-loaded comments. trafilatura is called with `include_comments=True`. The `_extract_reddit_comments()` JS function attempts DOM extraction as a fallback, but Reddit's virtualized comment UI means only ~30-40 top comments are typically captured (thousands more require clicking "Load more" which triggers XHR API calls). old.reddit.com often blocks headless browsers with bot detection; m.reddit.com works but loads fewer comments.

## External references

- Patchright: <https://github.com/Kaliiiiiiiiii-Vinyzu/patchright>
- Trafilatura: <https://trafilatura.readthedocs.io>
- MCP spec: <https://modelcontextprotocol.io>
- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>
- LM Studio MCP docs: <https://lmstudio.ai/docs/app/mcp>

## Scope discipline

No caching, no proxy rotation, no auth, no rate limiting, no PDF handling, no LAN exposure, no multi-user support. This is personal-use software. Do not volunteer "production-grade" additions. If the user asks for one, implement that one — not a framework around it.

## House style

- Match the terse register of the existing files: no docstrings on obvious functions, no decorative type annotations, no speculative abstractions, no feature flags.
- Three similar lines beat a premature abstraction.
- Keep the shim thin. If a feature can live in the container, put it there.
- Log at INFO sparingly — one line per operation. Tracebacks at WARNING for handled errors, ERROR for unhandled.
- Don't add error handling for conditions that can't happen. Validate at the HTTP and MCP boundaries only.
