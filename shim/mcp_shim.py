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
from mcp.server.fastmcp.utilities.types import Image

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
async def deep_dive_screenshot(urls: list[str], timeout_s: int = 30) -> list[Image]:
    """Fetch one or more URLs and return a full-page screenshot for each.

    Forwards to the deep-dive-scraper Docker container (must be running).
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
        return [Image(data=b"error", format="png") for _ in urls]
    except httpx.HTTPStatusError as e:
        log.warning("scraper service error: %s", e)
        return [Image(data=b"error", format="png") for _ in urls]

    images = []
    for item in results:
        if item.get("status") == "ok" and item.get("screenshot_b64"):
            raw = base64.b64decode(item["screenshot_b64"])
            images.append(Image(data=raw))
        else:
            images.append(Image(data=b"error", format="png"))
    return images


if __name__ == "__main__":
    mcp.run(transport="stdio")
