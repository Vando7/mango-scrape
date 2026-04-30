"""Thin stdio MCP shim that forwards deep_dive calls to the scraper HTTP service.

LM Studio (on Windows) spawns this via stdio. If the scraper server isn't already
running, the shim auto-starts it locally on port 8765. If the server has idle-shut
between tool calls, the next call respawns it transparently.

All tool responses are flat key=value strings — no JSON, no brackets,
no indentation. Multi-line values keep real newlines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("deep-dive-shim")

SERVICE_URL = os.environ.get("DEEP_DIVE_URL", "http://localhost:8765")

_parsed_url = urlparse(SERVICE_URL)
SCRAPER_PORT = _parsed_url.port or 8765
SCRAPER_HOST = _parsed_url.hostname or "localhost"
SERVER_SCRIPT = str(Path(__file__).resolve().parent.parent / "server.py")
VENV_PYTHON = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")

mcp = FastMCP("deep-dive")


def _fmt(r: dict) -> str:
    """Format a single URL result as flat key=value lines.

    Multi-line values keep real newlines — no escape artifacts for the model.
    """
    return "\n".join(f"{k}={v}" for k, v in r.items())


def _fmt_list(data: dict) -> str:
    """Format a {status, query, num_results, results: [...]} dict as flat lines.

    Each result is preceded by a `===` divider. On non-ok status, falls back
    to single-block _fmt formatting.
    """
    if data.get("status") != "ok":
        return _fmt(data)
    parts = [f"status={data['status']}", f"query={data['query']}", f"num_results={data['num_results']}"]
    for r in data.get("results", []):
        parts.append("===")
        for k, v in r.items():
            parts.append(f"{k}={v}")
    return "\n".join(parts)


class ServiceError(Exception):
    """Raised when the scraper service is unreachable or returns a non-2xx."""


async def _post(path: str, payload: dict, timeout: int = 60):
    """POST to the scraper service. On ConnectError, try to (re)start the
    server once and retry. Raises ServiceError on persistent failure."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{SERVICE_URL}{path}", json=payload)
                r.raise_for_status()
                return r.json()
        except httpx.ConnectError as e:
            if attempt == 0:
                log.warning("scraper unreachable (%s) — attempting restart", e)
                await _ensure_server()
                continue
            raise ServiceError(f"service unreachable at {SERVICE_URL}")
        except httpx.HTTPStatusError as e:
            raise ServiceError(f"{e.response.status_code}: {e.response.text[:200]}")


@mcp.tool()
async def get_date() -> str:
    """Return today's date in ISO format (YYYY-MM-DD).

    Use to ground searches or answers in the current date — you don't have a
    system clock and your training data is older than today.
    """
    return date.today().isoformat()


@mcp.tool()
async def deep_dive(urls: list[str], timeout_s: int = 30) -> str:
    """Fetch one or more web pages and return cleaned article text + metadata.

    Use this to read the full content of a page after a search returns a
    snippet you want to dig into. For YouTube watch URLs prefer
    get_youtube_transcript (otherwise you get page chrome, not the spoken
    content). For pages where layout/visuals matter (charts, dashboards,
    rendered apps), use deep_dive_screenshot instead.

    Each URL gets its own block separated by `===`. Per-URL fields:
        url, status, title, author, date, description, markdown_content,
        word_count, links.
    Failures are reported per URL (status=error, error=<reason>) — other URLs
    in the same call still succeed, so always check `status` per block.

    Args:
        urls: One or more http(s) URLs.
        timeout_s: Per-URL navigation timeout in seconds. Default 30.
    """
    if not urls:
        return ""
    try:
        results = await _post(
            "/scrape",
            {"urls": urls, "timeout_s": timeout_s},
            timeout=max(60, timeout_s * len(urls) + 30),
        )
    except ServiceError as e:
        results = [{"url": u, "status": "error", "error": str(e)} for u in urls]
    return "\n===\n".join(_fmt(r) for r in results)


@mcp.tool()
async def deep_dive_screenshot(urls: list[str], timeout_s: int = 30) -> list[ImageContent | TextContent]:
    """Capture full-page screenshots of one or more URLs.

    Use when text scraping isn't enough — visual layout, charts, dashboards,
    maps, or pages that render heavily client-side. Screenshots are emitted
    as MCP ImageContent blocks (PNG); whether the model actually sees pixels
    or a base64 string depends on the MCP client's image support — only call
    this if you know your runtime renders ImageContent properly.

    One block per URL: ImageContent on success, TextContent with the error
    message on failure. A failed URL doesn't fail the whole call.

    Args:
        urls: One or more http(s) URLs.
        timeout_s: Per-URL navigation timeout in seconds. Default 30.
    """
    if not urls:
        return []
    try:
        results = await _post(
            "/screenshot",
            {"urls": urls, "timeout_s": timeout_s},
            timeout=max(60, timeout_s * len(urls) + 30),
        )
    except ServiceError as e:
        return [
            TextContent(type="text", text=f"screenshot failed for {u}: {e}")
            for u in urls
        ]

    out: list[ImageContent | TextContent] = []
    for item in results:
        if item.get("status") == "ok" and item.get("screenshot_b64"):
            out.append(
                ImageContent(type="image", data=item["screenshot_b64"], mimeType="image/png")
            )
        else:
            out.append(
                TextContent(
                    type="text",
                    text=f"screenshot failed for {item.get('url', '?')}: {item.get('error', 'unknown')}",
                )
            )
    return out


@mcp.tool()
async def get_youtube_transcript(urls: list[str], timeout_s: int = 30, language: str = "en") -> str:
    """Fetch YouTube transcripts + video metadata for one or more URLs.

    Use this for any youtube.com/watch or youtu.be URL — calling deep_dive on
    a YouTube page just returns the page chrome, not the spoken content.
    Transcripts are cached, so re-calls are instant.

    Long transcripts are paginated (~5k words/page); page 1 is included with
    total_pages, fetch follow-up pages with deep_dive_transcript_page using
    the same language.

    Each URL gets its own block separated by `===`. Per-URL fields:
        url, status, video_id, title, channel, upload_date, description,
        markdown_content, word_count, transcript_status, comment_count.

    Args:
        urls: One or more YouTube video URLs.
        timeout_s: Per-URL timeout in seconds. Default 30.
        language: Transcript language code. Default 'en'. Single ('bg') or
                  comma-separated preference list ('en,bg'). If none of the
                  requested languages is available, falls back through common
                  languages (es, de, fr, pt, ru, ja, ko, zh-Hans, bg).
    """
    if not urls:
        return ""
    try:
        results = await _post(
            "/scrape_youtube",
            {"urls": urls, "timeout_s": timeout_s, "language": language},
            timeout=max(60, timeout_s * len(urls) + 30),
        )
    except ServiceError as e:
        results = [{"url": u, "status": "error", "error": str(e)} for u in urls]
    return "\n===\n".join(_fmt(r) for r in results)


@mcp.tool()
async def download_file(url: str) -> str:
    """Download a file from a URL into the shared workspace.

    Use for binaries, archives, PDFs, images, datasets — anything you'd want
    to keep around or process later, not just read once as HTML. The file
    lands in the shared workspace directory; afterwards browse with
    list_files and read text contents with cat_file.

    Returns flat key=value lines: status, path, filename, size, mime_type.
    """
    try:
        return _fmt(await _post("/download", {"url": url}, timeout=60))
    except ServiceError as e:
        return _fmt({"status": "error", "url": url, "error": str(e)})


@mcp.tool()
async def clone_repo(git_url: str) -> str:
    """Clone a public git repository into the shared workspace.

    Use to bring an entire codebase in for inspection — afterwards browse
    with list_files and read individual files with cat_file. The repo lives
    at workspace/<repo_name>. Public URLs only; no credential handling.

    Returns flat key=value lines: status, path, repo_name, file_count.
    """
    try:
        return _fmt(await _post("/clone", {"git_url": git_url}, timeout=120))
    except ServiceError as e:
        return _fmt({"status": "error", "git_url": git_url, "error": str(e)})


@mcp.tool()
async def list_files(path: str = ".") -> str:
    """List files and directories in the shared workspace, recursively.

    The workspace holds whatever was brought in by download_file or
    clone_repo. Pair with cat_file to actually read anything you find. Output
    is capped at 500 entries.

    Args:
        path: Subpath inside the workspace. Default '.' (workspace root).

    Returns flat key=value lines: status, path, total_entries, entries.
    """
    try:
        return _fmt(await _post("/list", {"path": path}, timeout=30))
    except ServiceError as e:
        return _fmt({"status": "error", "path": path, "error": str(e)})


@mcp.tool()
async def cat_file(path: str) -> str:
    """Read the text content of a file inside the shared workspace.

    Reads files brought in via download_file or clone_repo. Text only —
    binary files will fail to decode. Content is capped at ~100k chars.

    Args:
        path: Path inside the workspace, as reported by list_files.

    Returns flat key=value lines: status, path, filename, size, content.
    """
    try:
        return _fmt(await _post("/cat", {"path": path}, timeout=30))
    except ServiceError as e:
        return _fmt({"status": "error", "path": path, "error": str(e)})


@mcp.tool()
async def web_search(
    query: str,
    num_results: int = 5,
    language: str = "",
    deep: bool = False,
) -> str:
    """Search the web via Brave Search.

    For HN- or Reddit-specific queries prefer hn_search / reddit_search —
    they return better-structured discussion metadata than a generic web
    search.

    Each result is separated by `===`. Per-result fields: url, title, snippet.

    When deep=True, additionally scrapes each result URL and merges the
    deep_dive output (markdown_content, description, links, etc.) into the
    same block. Use sparingly — every result is fetched, which can take 30s+
    and consume a lot of context. Prefer the default (snippet-only) and call
    deep_dive only on the URLs you actually want to read.

    Args:
        query: Search query string (max 400 chars).
        num_results: 1-20. Default 5.
        language: Language code (e.g. 'en', 'de'). Empty for default.
        deep: If True, scrape each result and inline its markdown.
    """
    try:
        data = await _post(
            "/search",
            {"query": query, "num_results": num_results, "language": language},
            timeout=30,
        )
    except ServiceError as e:
        return _fmt({"status": "error", "query": query, "error": str(e)})

    if data.get("status") != "ok":
        return _fmt(data)

    if deep and data.get("results"):
        urls = [r["url"] for r in data["results"]]
        try:
            scrapes = await _post(
                "/scrape",
                {"urls": urls, "timeout_s": 30},
                timeout=max(60, 30 * len(urls) + 30),
            )
        except ServiceError as e:
            scrapes = [{"url": u, "status": "error", "error": str(e)} for u in urls]
        by_url = {s["url"]: s for s in scrapes}
        for r in data["results"]:
            sc = by_url.get(r["url"], {})
            for k, v in sc.items():
                if k == "url":
                    continue
                if k == "title" and not v:
                    continue  # keep search title if scrape didn't extract one
                r[k] = v

    return _fmt_list(data)


@mcp.tool()
async def deep_dive_transcript_page(
    video_id: str,
    page_num: int = 1,
    language: str = "en",
) -> str:
    """Fetch one page of a YouTube transcript by page number.

    Follow-up to get_youtube_transcript when total_pages > 1. The transcript
    is cached after the first call, so subsequent pages are instant. The
    `language` argument must match the language used in the original call —
    otherwise the cache misses and the fetch starts over.

    Args:
        video_id: 11-char YouTube video ID (from the watch URL).
        page_num: 1-indexed page number, must be ≤ total_pages.
        language: Language code. Must match the language returned by
                  get_youtube_transcript for this video.

    Returns flat key=value lines: status, video_id, language, cached,
    total_pages, page_num, page_size, transcript.
    """
    try:
        return _fmt(await _post(
            "/transcript_page",
            {"video_id": video_id, "page_num": page_num, "language": language},
            timeout=60,
        ))
    except ServiceError as e:
        return _fmt({"status": "error", "video_id": video_id, "page_num": page_num, "error": str(e)})


@mcp.tool()
async def hn_search(
    query: str,
    num_results: int = 10,
    tags: str = "story",
    sort_by_date: bool = False,
) -> str:
    """Search Hacker News via the Algolia API. Free, fast, no auth.

    Triage tool — to read the full thread or linked article, follow up with
    deep_dive on `hn_url` (discussion) or `url` (external link).

    One block per result separated by `===`. Per-result fields: title,
    url (external link, or the HN item URL for Ask HN posts), hn_url
    (HN discussion thread), author, points, num_comments, created.

    Args:
        query: Search query.
        num_results: 1-50. Default 10.
        tags: Algolia filter — "story" (default), "comment", "story,comment",
              "front_page", "ask_hn", "show_hn", or "author_<username>".
        sort_by_date: If True, sort by date (newest first) instead of relevance.
    """
    try:
        data = await _post(
            "/hn_search",
            {
                "query": query,
                "num_results": num_results,
                "tags": tags,
                "sort_by_date": sort_by_date,
            },
            timeout=20,
        )
    except ServiceError as e:
        return _fmt({"status": "error", "query": query, "error": str(e)})
    return _fmt_list(data)


@mcp.tool()
async def reddit_search(
    query: str,
    num_results: int = 10,
    subreddit: str = "",
    sort: str = "relevance",
    time: str = "all",
) -> str:
    """Search Reddit via the public JSON endpoint.

    Triage tool — to read the full thread including comments, follow up with
    deep_dive on `permalink` (the scraper handles Reddit's bot detection).

    One block per result separated by `===`. Per-result fields: title,
    selftext_preview (first 500 chars of self-post body), url (external link
    or the post itself), permalink (discussion thread on reddit.com),
    subreddit, author, score, num_comments, created (epoch seconds).

    Reddit aggressively rate-limits unauthenticated requests; on 429/403,
    scope the query to a specific subreddit instead of retrying blindly.

    Args:
        query: Search query.
        num_results: 1-25. Default 10.
        subreddit: Restrict to one subreddit (e.g. "python"). Default = all.
        sort: "relevance" (default), "hot", "top", "new", "comments".
        time: "hour", "day", "week", "month", "year", "all" (default).
    """
    try:
        data = await _post(
            "/reddit_search",
            {
                "query": query,
                "num_results": num_results,
                "subreddit": subreddit,
                "sort": sort,
                "time": time,
            },
            timeout=20,
        )
    except ServiceError as e:
        return _fmt({"status": "error", "query": query, "error": str(e)})
    return _fmt_list(data)


async def _server_healthy(url: str) -> bool:
    """Check if the scraper server responds to /health."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{url}/health")
            return r.status_code == 200
    except Exception:
        return False


async def _ensure_server():
    """Start the scraper server if it's not already running."""
    if await _server_healthy(SERVICE_URL):
        log.info("scraper server already running at %s", SERVICE_URL)
        return

    if SCRAPER_HOST not in ("localhost", "127.0.0.1"):
        log.warning("DEEP_DIVE_URL points to non-local host %s — not auto-starting", SCRAPER_HOST)
        return

    log.warning("scraper server not running — starting locally on port %d", SCRAPER_PORT)

    # Find python: prefer .venv, fall back to system python
    py = VENV_PYTHON if os.path.exists(VENV_PYTHON) else "python"
    work_dir = str(Path(__file__).resolve().parent.parent)

    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "server:app", f"--port={SCRAPER_PORT}"],
        cwd=work_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait up to 30s for the server to come up
    for _ in range(60):
        if await _server_healthy(SERVICE_URL):
            log.info("scraper server started (pid %d)", proc.pid)
            return
        await asyncio.sleep(0.5)

    # If we get here, the server failed to start — don't block the shim.
    log.error("failed to start scraper server (exit code %s)", proc.returncode)
    # Leave SERVICE_URL as-is so tool calls will return error messages instead of hanging.


if __name__ == "__main__":
    asyncio.run(_ensure_server())
    mcp.run(transport="stdio")
