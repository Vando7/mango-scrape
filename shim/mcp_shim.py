"""Thin stdio MCP shim that forwards deep_dive calls to the Docker HTTP service.

LM Studio (on Windows) spawns this via stdio; it HTTP-POSTs into the
deep-dive-scraper container running on WSL.

All tool responses are flat key=value strings — no JSON, no brackets,
no indentation. Multi-line values have internal newlines escaped as \n.
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

SERVICE_URL = os.environ.get("DEEP_DIVE_URL", "http://localhost:9161")

mcp = FastMCP("deep-dive")


def _fmt(r: dict) -> str:
    """Format a single URL result as flat key=value lines.

    Multi-line values (markdown_content, description, transcript) have
    internal newlines escaped as literal \\n so the model sees one line per field.
    """
    parts = []
    for k, v in r.items():
        if isinstance(v, str):
            # Escape backslashes first, then real newlines → literal \n
            val = v.replace("\\", "\\\\").replace("\n", "\\n")
            parts.append(f"{k}={val}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}={v}")
    return "\n".join(parts)


@mcp.tool()
async def get_date() -> str:
    """Return today's date."""
    return date.today().isoformat()


@mcp.tool()
async def deep_dive(urls: list[str], timeout_s: int = 30) -> str:
    """Fetch one or more URLs and return clean markdown + metadata for each.

    Forwards to the deep-dive-scraper Docker container (must be running).
    Returns flat key=value lines — no JSON, no brackets.
    """
    if not urls:
        return ""
    client_timeout = max(60, timeout_s * len(urls) + 30)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SERVICE_URL}/scrape",
                json={"urls": urls, "timeout_s": timeout_s},
            )
            r.raise_for_status()
            results = r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        results = [
            {
                "url": u,
                "status": "error",
                "error": f"scraper service unreachable at {SERVICE_URL} — is `docker compose up` running?",
            }
            for u in urls
        ]
    except httpx.HTTPStatusError as e:
        log.warning("scraper service error: %s", e)
        results = [
            {
                "url": u,
                "status": "error",
                "error": f"service {e.response.status_code}: {e.response.text[:200]}",
            }
            for u in urls
        ]

    # Separate blocks per URL with a divider
    return "\n---\n".join(_fmt(r) for r in results)


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
async def get_youtube_transcript(urls: list[str], timeout_s: int = 30) -> str:
    """Fetch YouTube video transcripts + metadata for one or more URLs.

    Forwards to the deep-dive-scraper Docker container (must be running).
    Returns flat key=value lines — no JSON, no brackets.
    Fields: url, status, video_id, title, channel, upload_date, description,
            markdown_content, word_count, transcript_status, comment_count.
    """
    if not urls:
        return ""
    client_timeout = max(60, timeout_s * len(urls) + 30)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SERVICE_URL}/scrape_youtube",
                json={"urls": urls, "timeout_s": timeout_s},
            )
            r.raise_for_status()
            results = r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        results = [
            {
                "url": u,
                "status": "error",
                "error": f"scraper service unreachable at {SERVICE_URL} — is `docker compose up` running?",
            }
            for u in urls
        ]
    except httpx.HTTPStatusError as e:
        log.warning("youtube scrape error: %s", e)
        results = [
            {
                "url": u,
                "status": "error",
                "error": f"service {e.response.status_code}: {e.response.text[:200]}",
            }
            for u in urls
        ]

    return "\n---\n".join(_fmt(r) for r in results)


@mcp.tool()
async def download_file(url: str) -> str:
    """Download a file from a URL into the workspace directory.

    Returns flat key=value lines with path, filename, size, mime_type.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{SERVICE_URL}/download",
                json={"url": url},
            )
            r.raise_for_status()
            return _fmt(r.json())
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return _fmt({"status": "error", "url": url, "error": f"service unreachable at {SERVICE_URL}"})
    except httpx.HTTPStatusError as e:
        log.warning("download error: %s", e)
        return _fmt({"status": "error", "url": url, "error": f"{e.response.status_code}: {e.response.text[:200]}"})


@mcp.tool()
async def clone_repo(git_url: str) -> str:
    """Clone a git repository into the workspace directory.

    Returns flat key=value lines with path, repo_name, file_count.
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{SERVICE_URL}/clone",
                json={"git_url": git_url},
            )
            r.raise_for_status()
            return _fmt(r.json())
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return _fmt({"status": "error", "git_url": git_url, "error": f"service unreachable at {SERVICE_URL}"})
    except httpx.HTTPStatusError as e:
        log.warning("clone error: %s", e)
        return _fmt({"status": "error", "git_url": git_url, "error": f"{e.response.status_code}: {e.response.text[:200]}"})


@mcp.tool()
async def list_files(path: str = ".") -> str:
    """List files and directories recursively in the workspace.

    Returns flat key=value lines with status, path, total_entries, entries.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SERVICE_URL}/list",
                json={"path": path},
            )
            r.raise_for_status()
            return _fmt(r.json())
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return _fmt({"status": "error", "path": path, "error": f"service unreachable at {SERVICE_URL}"})
    except httpx.HTTPStatusError as e:
        log.warning("list error: %s", e)
        return _fmt({"status": "error", "path": path, "error": f"{e.response.status_code}: {e.response.text[:200]}"})


@mcp.tool()
async def cat_file(path: str) -> str:
    """Read and display the text content of a file in the workspace.

    Returns flat key=value lines with status, path, filename, size, content.
    Content is multi-line; internal newlines escaped as \\n.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SERVICE_URL}/cat",
                json={"path": path},
            )
            r.raise_for_status()
            return _fmt(r.json())
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return _fmt({"status": "error", "path": path, "error": f"service unreachable at {SERVICE_URL}"})
    except httpx.HTTPStatusError as e:
        log.warning("cat error: %s", e)
        return _fmt({"status": "error", "path": path, "error": f"{e.response.status_code}: {e.response.text[:200]}"})


if __name__ == "__main__":
    mcp.run(transport="stdio")
