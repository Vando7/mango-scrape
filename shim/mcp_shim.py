"""Thin stdio MCP shim that forwards deep_dive calls to the Docker HTTP service.

LM Studio (on Windows) spawns this via stdio; it HTTP-POSTs into the
deep-dive-scraper container running on WSL.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from datetime import date

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("deep-dive-shim")

SERVICE_URL = os.environ.get("DEEP_DIVE_URL", "http://localhost:8765")

mcp = FastMCP("deep-dive")


@mcp.tool()
async def get_date() -> str:
    """Return today's date."""
    return date.today().isoformat()


@mcp.tool()
async def deep_dive(urls: list[str], timeout_s: int = 30) -> list[dict]:
    """Fetch one or more URLs and return clean markdown + metadata for each.

    Forwards to the deep-dive-scraper Docker container (must be running).
    """
    if not urls:
        return []
    # Pad the HTTP timeout so per-URL browser timeouts can expire server-side first.
    client_timeout = max(60, timeout_s * len(urls) + 30)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SERVICE_URL}/scrape",
                json={"urls": urls, "timeout_s": timeout_s},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return [
            {
                "url": u,
                "status": "error",
                "error": f"scraper service unreachable at {SERVICE_URL} — is `docker compose up` running?",
            }
            for u in urls
        ]
    except httpx.HTTPStatusError as e:
        log.warning("scraper service error: %s", e)
        return [
            {
                "url": u,
                "status": "error",
                "error": f"service {e.response.status_code}: {e.response.text[:200]}",
            }
            for u in urls
        ]


@mcp.tool()
async def deep_dive_screenshot(urls: list[str], timeout_s: int = 30) -> list[ImageContent]:
    """Fetch one or more URLs and return a full-page screenshot for each.

    Forwards to the deep-dive-scraper Docker container (must be running).
    Returns ImageContent blocks — the model sees actual images, not base64 strings.
    """
    if not urls:
        return []
    client_timeout = max(60, timeout_s * len(urls) + 30)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SERVICE_URL}/screenshot",
                json={"urls": urls, "timeout_s": timeout_s},
            )
            r.raise_for_status()
            results = r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return [
            ImageContent(type="image", data=base64.b64encode(b"error").decode(), mimeType="image/png")
            for _ in urls
        ]
    except httpx.HTTPStatusError as e:
        log.warning("scraper service error: %s", e)
        return [
            ImageContent(type="image", data=base64.b64encode(b"error").decode(), mimeType="image/png")
            for _ in urls
        ]

    images = []
    for item in results:
        if item.get("status") == "ok" and item.get("screenshot_b64"):
            images.append(
                ImageContent(type="image", data=item["screenshot_b64"], mimeType="image/png")
            )
        else:
            images.append(
                ImageContent(type="image", data=base64.b64encode(b"error").decode(), mimeType="image/png")
            )
    return images


@mcp.tool()
async def get_youtube_transcript(urls: list[str], timeout_s: int = 30) -> list[dict]:
    """Fetch YouTube video transcripts + metadata for one or more URLs.

    Forwards to the deep-dive-scraper Docker container (must be running).
    Returns structured data including:
      - title: Full video title from page <title> tag
      - channel: Channel name extracted from meta tags / JSON-LD
      - upload_date: ISO 8601 date string from YouTube metadata
      - description: Video description text (truncated in markdown_content)
      - transcript_status: 'ok' if transcript was fetched, 'error' otherwise
      - word_count: Total words across all returned content
      - markdown_content: Full transcript text under '# Transcript' heading
    
    Note: Uses youtube-transcript-api for caption tracks (no browser needed).
    Comments are not included — this tool focuses on transcript + metadata only.
    """
    if not urls:
        return []
    client_timeout = max(60, timeout_s * len(urls) + 30)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SERVICE_URL}/scrape_youtube",
                json={"urls": urls, "timeout_s": timeout_s},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return [
            {
                "url": u,
                "status": "error",
                "error": f"scraper service unreachable at {SERVICE_URL} — is `docker compose up` running?",
            }
            for u in urls
        ]
    except httpx.HTTPStatusError as e:
        log.warning("youtube scrape error: %s", e)
        return [
            {
                "url": u,
                "status": "error",
                "error": f"service {e.response.status_code}: {e.response.text[:200]}",
            }
            for u in urls
        ]


@mcp.tool()
async def download_file(url: str) -> dict:
    """Download a file from a URL into the workspace directory.

    The file is saved using its name derived from the URL. Returns metadata
    including path, filename, size, and MIME type.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{SERVICE_URL}/download",
                json={"url": url},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return {"status": "error", "url": url, "error": f"service unreachable at {SERVICE_URL}"}
    except httpx.HTTPStatusError as e:
        log.warning("download error: %s", e)
        return {"status": "error", "url": url, "error": f"{e.response.status_code}: {e.response.text[:200]}"}


@mcp.tool()
async def clone_repo(git_url: str) -> dict:
    """Clone a git repository into the workspace directory.

    The repo is cloned using its name derived from the URL. Returns metadata
    including path, repo name, and file count.
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{SERVICE_URL}/clone",
                json={"git_url": git_url},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return {"status": "error", "git_url": git_url, "error": f"service unreachable at {SERVICE_URL}"}
    except httpx.HTTPStatusError as e:
        log.warning("clone error: %s", e)
        return {"status": "error", "git_url": git_url, "error": f"{e.response.status_code}: {e.response.text[:200]}"}


@mcp.tool()
async def list_files(path: str = ".") -> dict:
    """List files and directories recursively in the workspace.

    Args:
        path: Path relative to the workspace directory (default '.').

    Returns a listing of all entries with their type, name, and size.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SERVICE_URL}/list",
                json={"path": path},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return {"status": "error", "path": path, "error": f"service unreachable at {SERVICE_URL}"}
    except httpx.HTTPStatusError as e:
        log.warning("list error: %s", e)
        return {"status": "error", "path": path, "error": f"{e.response.status_code}: {e.response.text[:200]}"}


@mcp.tool()
async def cat_file(path: str) -> dict:
    """Read and display the text content of a file in the workspace.

    Args:
        path: Path to the file relative to the workspace directory.

    Returns the file's content (capped at ~100k chars) along with metadata.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SERVICE_URL}/cat",
                json={"path": path},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return {"status": "error", "path": path, "error": f"service unreachable at {SERVICE_URL}"}
    except httpx.HTTPStatusError as e:
        log.warning("cat error: %s", e)
        return {"status": "error", "path": path, "error": f"{e.response.status_code}: {e.response.text[:200]}"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
