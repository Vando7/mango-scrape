"""Thin stdio MCP shim that forwards deep_dive calls to the scraper HTTP service.

LM Studio (on Windows) spawns this via stdio. If the scraper server isn't already
running, the shim auto-starts it locally on port 8765.

All tool responses are flat key=value strings — no JSON, no brackets,
no indentation. Multi-line values keep real newlines.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

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

SCRAPER_PORT = 8765
SERVER_SCRIPT = str(Path(__file__).resolve().parent.parent / "server.py")
VENV_PYTHON = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")

mcp = FastMCP("deep-dive")


def _fmt(r: dict) -> str:
    """Format a single URL result as flat key=value lines.

    Multi-line values keep real newlines — no escape artifacts for the model.
    """
    parts = []
    for k, v in r.items():
        if isinstance(v, str):
            val = v.replace("\\", "\\\\")
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

    Auto-starts the scraper server if not already running. Returns flat key=value lines.
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
                "error": f"scraper service unreachable at {SERVICE_URL}",
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

    Auto-starts the scraper server if not already running. Returns ImageContent blocks.
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
async def get_youtube_transcript(urls: list[str], timeout_s: int = 30, language: str = "en") -> str:
    """Fetch YouTube video transcripts + metadata for one or more URLs.

    Auto-starts the scraper server if not already running. Returns flat key=value lines.

    Long transcripts are paginated (~5k words/page). The response includes
    total_pages and page info. Use deep_dive_transcript_page for subsequent pages.

    Args:
        urls: List of YouTube video URLs.
        timeout_s: Request timeout in seconds.
        language: Language code(s) for transcript. Default 'en'.
                  Supports single codes ('bg') or comma-separated ('en,bg').

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
                json={"urls": urls, "timeout_s": timeout_s, "language": language},
            )
            r.raise_for_status()
            results = r.json()
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        results = [
            {
                "url": u,
                "status": "error",
                "error": f"scraper service unreachable at {SERVICE_URL}",
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


@mcp.tool()
async def deep_dive_transcript_page(
    video_id: str,
    page_num: int = 1,
    language: str = "en",
) -> str:
    """Fetch a single transcript page for a YouTube video.

    Use after get_youtube_transcript to retrieve subsequent pages of long transcripts.
    Returns flat key=value lines with status, video_id, language, cached,
    total_pages, page_num, page_size, transcript.

    Args:
        video_id: YouTube video ID (11-char string).
        page_num: Page number (1-indexed). Default 1.
        language: Language code. Default 'en'.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{SERVICE_URL}/transcript_page",
                json={"video_id": video_id, "page_num": page_num, "language": language},
            )
            r.raise_for_status()
            return _fmt(r.json())
    except httpx.ConnectError as e:
        log.warning("scraper unreachable: %s", e)
        return _fmt({"status": "error", "video_id": video_id, "page_num": page_num,
                     "error": f"service unreachable at {SERVICE_URL}"})
    except httpx.HTTPStatusError as e:
        log.warning("transcript_page error: %s", e)
        return _fmt({"status": "error", "video_id": video_id, "page_num": page_num,
                     "error": f"{e.response.status_code}: {e.response.text[:200]}"})


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
