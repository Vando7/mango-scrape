"""Deep-dive MCP server: scrapes URLs and returns clean markdown + metadata.

Stdio hygiene — stdout must contain ONLY JSON-RPC. All logging goes to stderr.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import Context, FastMCP

from scraper import launch_browser, scrape_many

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("deep-dive")


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    log.info("launching patchright browser")
    browser, pw = await launch_browser()
    try:
        yield {"browser": browser}
    finally:
        log.info("closing patchright browser")
        try:
            await browser.close()
        finally:
            await pw.stop()


mcp = FastMCP("deep-dive", lifespan=lifespan)


@mcp.tool()
async def deep_dive(urls: list[str], ctx: Context, timeout_s: int = 30) -> list[dict]:
    """Fetch one or more URLs and return clean markdown + metadata for each.

    Renders each page in a stealth Chromium (patchright) to bypass basic bot
    detection, then extracts the main content with trafilatura.

    Args:
        urls: One or more URLs to scrape.
        timeout_s: Per-URL navigation timeout in seconds (default 30).

    Returns:
        One dict per URL with keys: url, status, title, author, date,
        description, markdown_content, word_count. On failure: url,
        status="error", error.
    """
    if not urls:
        return []
    browser = ctx.request_context.lifespan_context["browser"]
    log.info("deep_dive on %d url(s)", len(urls))
    return await scrape_many(browser, urls, timeout_s)


if __name__ == "__main__":
    mcp.run(transport="stdio")
