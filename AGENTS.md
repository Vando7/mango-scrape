# AGENTS.md

Personal-use "deep dive" scraper for local LLMs. Takes URLs, returns clean markdown + metadata. Intended workflow: a local model gets search snippets (e.g. from SearXNG), then calls `deep_dive` on results it wants to read in full.

## Architecture

```
LM Studio
  │  spawns via stdio
  ▼
mcp_shim.py (thin, self-bootstraps) ──HTTP──▶ localhost:8765
                                                  │
                                                  ▼
                                            server.py (FastAPI)
                                                  │
                                                  ▼
                                            scraper.py (patchright + trafilatura)
```

The shim auto-starts the FastAPI server on port 8765 if it isn't already running. No Docker needed — all deps live in `.venv`. The shim is intentionally thin: stdio MCP in, HTTP POST out.

## Files

| Path | Role |
|---|---|
| `scraper.py` | Scraping logic. `_compact`, `launch_browser`, `_scroll_page`, `_extract_reddit_comments`, `scrape_one`, `scrape_many`. YouTube: delegates transcript to `yt_transcript`. Reddit-specific: scroll + network idle wait + include_comments=True + www→old.reddit→m.reddit fallback chain. |
| `server.py` | FastAPI wrapper. Lifespan owns a shared browser. `POST /scrape`, `POST /screenshot`, `GET /health`, `POST /scrape_youtube`, `POST /transcript_page`. Returns compact JSON. Default workspace: `C:\software\searchmcp\scraper\scrape\workspace`. |
| `yt_transcript.py` | YouTube transcript fetching with SQLite cache and word-based pagination (500-word pages). Functions: `get_transcript`, `paginate_transcript`, `get_page`, `cache_get`, `cache_put`. |
| `shim/mcp_shim.py` | stdio MCP server. `_fmt()` converts dicts → flat key=value strings (no JSON, no brackets). Multi-line values keep real newlines — no escape artifacts. Auto-starts scraper if needed. Forwards all tool calls to local HTTP service. |
| `shim/pyproject.toml` | Shim deps: `mcp`, `httpx`. |
| `pyproject.toml` | Host-side dev deps (for running `server.py`). |
| `README.md` | Human-facing setup. |

## Commands

```bash
# One-shot: shim auto-starts the server on first call
uv run python shim/mcp_shim.py

# Manual: start server only (shim will do it automatically anyway)
cd C:\software\searchmcp\scraper\scrape
uv sync                          # installs deps + managed Python into .venv
uv run patchright install chromium  # downloads patched Chromium binary
uv run uvicorn server:app --port 8765

# Smoke tests (server must be running)
curl -s http://localhost:8765/health
curl -s -X POST http://localhost:8765/scrape -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"]}' | jq .

# Stop (if started manually)
taskkill /F /IM uv.exe
```

## Conventions and gotchas

**Stdio MCP hygiene (shim).** The shim speaks JSON-RPC over stdout. Never `print()` in `mcp_shim.py` or anything it imports. All logging to stderr (`logging.basicConfig(stream=sys.stderr, ...)`). A stray stdout byte corrupts the protocol and the client closes the connection silently.

**Use patchright, not playwright.** Import is `from patchright.async_api import …`. Patchright patches the `Runtime.enable` CDP leak that regular Playwright (and `playwright-stealth`) can't mask. Chromium-only. Do not "upgrade" to vanilla Playwright — you lose stealth.

**Patchright's Chromium is separate.** `pip install patchright` does not fetch a browser. `patchright install chromium` downloads a patched build distinct from any existing Playwright browser. Run once: `uv run patchright install chromium`.

**Shared browser, fresh context per URL.** `server.py` launches one browser in `lifespan` and reuses it. Each scrape creates a new `browser.new_context()` (cheap) and closes it. Do not launch per request (1–2 s overhead). Do not reuse contexts (leaks cookies/state).

**Errors are per-URL, never raise.** `scrape_one` catches everything and returns `{status: "error", error: ...}`. `scrape_many` uses `asyncio.gather(..., return_exceptions=True)`. Callers always get one result per input URL — preserve this contract.

**Self-bootstrapping shim.** The shim checks `localhost:8765/health` on startup. If the server isn't running, it launches uvicorn as a subprocess and waits up to 30s for readiness. Falls back gracefully if launch fails (tool calls return error messages instead of hanging).

**LM Studio mcp.json.** Located at `%USERPROFILE%\.lmstudio\mcp.json` on Windows. Follows Cursor's notation: `{"mcpServers": {...}}`. LM Studio spawns the shim via stdio — no env vars needed, server auto-starts. Example:
```json
{
  "mcpServers": {
    "deep-dive": {
      "command": "C:\\software\\searchmcp\\scraper\\scrape\\.venv\\Scripts\\python.exe",
      "args": ["shim/mcp_shim.py"]
    }
  }
}
```
Per-server stderr is visible in the Program tab.

**Screenshots return MCP ImageContent.** `deep_dive_screenshot` in the shim forwards screenshot b64 from the server into `mcp.types.ImageContent(type="image", data=<b64>, mimeType="image/png")` blocks. These serialize directly as image content blocks with base64 data and mime type — the model sees actual images, not opaque strings.

**YouTube transcript pagination.** Long transcripts are split into ~500-word pages. `get_youtube_transcript` returns page 1 + metadata (`total_pages`, `page_num`). The model is told to say "next" for more pages. Use `deep_dive_transcript_page(video_id, page_num)` to fetch subsequent pages. Transcripts are cached in SQLite (`./cache/yt_transcripts.db`) keyed by `(video_id, language)` — second call hits cache instantly.

**Reddit scraping.** For `reddit.com` URLs (not old.reddit or m.reddit), scraper.py tries: **old.reddit first** (cleanest HTML, no heavy JS), then www, then m.reddit. `_inject_stealth()` runs before navigation to override `navigator.webdriver`, `window.chrome`, plugins, languages, and hardwareConcurrency — bypassing old.reddit's bot detection. Realistic headers (`Accept`, `Sec-Fetch-*`, etc.) are set via context-level `extra_http_headers`. On www.reddit.com it waits longer for network idle (`timeout_s * 500ms`) and scrolls the page to trigger lazy-loaded comments. trafilatura is called with `include_comments=True`. The `_extract_reddit_comments()` JS function attempts DOM extraction as a fallback, but Reddit's virtualized comment UI means only ~30-40 top comments are typically captured (thousands more require clicking "Load more" which triggers XHR API calls). m.reddit.com works but loads fewer comments.

## House style

- Match the terse register of the existing files: no docstrings on obvious functions, no decorative type annotations, no speculative abstractions, no feature flags.
- Three similar lines beat a premature abstraction.
- Keep the shim thin. If a feature can live in the server, put it there.
- Log at INFO sparingly — one line per operation. Tracebacks at WARNING for handled errors, ERROR for unhandled.
- Don't add error handling for conditions that can't happen. Validate at the HTTP and MCP boundaries only.
