"""Patchright + trafilatura: stealth browser -> clean markdown."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import trafilatura
from patchright.async_api import Browser, async_playwright

log = logging.getLogger("deep-dive.scraper")


async def launch_browser() -> tuple[Browser, Any]:
    """Start patchright and launch a headless Chromium. Returns (browser, playwright_ctx)."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return browser, pw


async def scrape_one(browser: Browser, url: str, timeout_s: int) -> dict:
    """Scrape one URL. Always returns a dict — errors are captured, never raised."""
    context = None
    html: str | None = None
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        # Best-effort settle; plenty of sites never hit networkidle, so swallow timeouts.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        html = await page.content()
    except Exception as e:
        log.warning("navigation failed: %s -> %s", url, e)
        return {"url": url, "status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    try:
        content = (
            trafilatura.extract(
                html,
                output_format="markdown",
                url=url,
                include_comments=False,
                include_tables=True,
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html)
    except Exception as e:
        log.warning("extraction failed: %s -> %s", url, e)
        return {
            "url": url,
            "status": "error",
            "error": f"extract: {type(e).__name__}: {e}",
        }

    return {
        "url": url,
        "status": "ok",
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "date": getattr(meta, "date", None),
        "description": getattr(meta, "description", None),
        "markdown_content": content,
        "word_count": len(content.split()) if content else 0,
    }


async def scrape_many(browser: Browser, urls: list[str], timeout_s: int) -> list[dict]:
    """Scrape URLs concurrently, one fresh browser context per URL."""
    results = await asyncio.gather(
        *[scrape_one(browser, u, timeout_s) for u in urls],
        return_exceptions=True,
    )
    out: list[dict] = []
    for url, r in zip(urls, results):
        if isinstance(r, Exception):
            out.append(
                {"url": url, "status": "error", "error": f"{type(r).__name__}: {r}"}
            )
        else:
            out.append(r)
    return out


async def screenshot_one(browser: Browser, url: str, timeout_s: int) -> dict:
    """Screenshot one URL. Always returns a dict — errors are captured, never raised."""
    context = None
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        screenshot = await page.screenshot(full_page=True)
    except Exception as e:
        log.warning("screenshot failed: %s -> %s", url, e)
        return {"url": url, "status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    return {
        "url": url,
        "status": "ok",
        "screenshot_b64": base64.b64encode(screenshot).decode(),
    }


async def screenshot_many(
    browser: Browser, urls: list[str], timeout_s: int
) -> list[dict]:
    """Screenshot URLs concurrently, one fresh browser context per URL."""
    results = await asyncio.gather(
        *[screenshot_one(browser, u, timeout_s) for u in urls],
        return_exceptions=True,
    )
    out: list[dict] = []
    for url, r in zip(urls, results):
        if isinstance(r, Exception):
            out.append(
                {"url": url, "status": "error", "error": f"{type(r).__name__}: {r}"}
            )
        else:
            out.append(r)
    return out
