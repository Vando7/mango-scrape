"""Patchright + trafilatura: stealth browser -> clean markdown."""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from patchright.async_api import Browser, async_playwright

log = logging.getLogger("deep-dive.scraper")


async def launch_browser() -> tuple[Browser, Any]:
    """Start patchright and launch a headless Chromium with stealth settings.

    Returns (browser, playwright_ctx).
    """
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    return browser, pw


async def _scroll_page(page, scroll_count: int = 10, delay_ms: int = 500):
    """Scroll down the page multiple times to trigger lazy-loaded content."""
    for i in range(scroll_count):
        try:
            await page.evaluate(f"window.scrollBy(0, {delay_ms * 3})")
            await asyncio.sleep(delay_ms / 1000)
        except Exception:
            break


async def _extract_reddit_comments(page) -> str:
    """Extract all visible comment text from a Reddit page via JS selectors."""
    try:
        comments = await page.evaluate("""
            (() => {
                const results = [];

                // Strategy 1: Extract from embedded JSON data (Reddit's internal state)
                let jsonComments = null;
                try {
                    // Try to find __NEXT_DATA__ or similar embedded JSON
                    const scripts = document.querySelectorAll('script[type="application/json"]');
                    for (const script of scripts) {
                        try {
                            const data = JSON.parse(script.textContent);
                            if (data?.payload?.graphql?.data?.comments?.nodes) {
                                jsonComments = data.payload.graphql.data.comments.nodes;
                                break;
                            }
                        } catch(e) {}
                    }
                } catch(e) {}

                // Strategy 2: Extract from DOM elements
                const selectors = [
                    '.usertext',
                    '[data-testid="comment-ui"]',
                    '[class*="CommentBlock"]',
                    '[class*="body__"]',
                    '[class*="md "]',
                ];

                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 10) {
                            let isDuplicate = false;
                            for (const r of results) {
                                if (r.startsWith(text.substring(0, 30))) { isDuplicate = true; break; }
                            }
                            if (!isDuplicate) results.push(text);
                        }
                    });
                });

                return results.join('\n\n---\n\n');
            })()
        """)
        return comments or ""
    except Exception as e:
        log.warning("reddit comment extraction failed: %s", e)
        return ""


async def _make_context(browser: Browser) -> Any:
    """Create a stealthy browser context with realistic fingerprints."""
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1920, "height": 1080},
    )
    return ctx


async def scrape_one(browser: Browser, url: str, timeout_s: int) -> dict:
    """Scrape one URL. Always returns a dict — errors are captured, never raised.

    For Reddit URLs, tries multiple strategies (mobile → old Reddit).
    """
    # Reddit-specific fallback chain: www first (more comments), then old.reddit, then m.reddit
    urls_to_try = [url]
    if "reddit.com" in url and not url.startswith("old.reddit") and not url.startswith("m.reddit"):
        base = url.split("?")[0].rstrip("/")
        # Extract path after domain (e.g. /r/python)
        parts = base.split("/", 3)  # ["https:", "", "www.reddit.com", "/r/python"]
        path = f"/{parts[3]}" if len(parts) > 3 else ""
        urls_to_try = [
            url,
            f"https://old.reddit.com{path}",
            f"https://m.reddit.com{path}",
        ]

    context: Any | None = None
    html: str | None = None
    last_error: str | None = None

    for attempt_url in urls_to_try:
        context = await _make_context(browser)
        page = await context.new_page()
        reddit_comments_html: str | None = None
        html: str | None = None
        last_error: str | None = None
        try:
            log.info("scrape attempt: %s", attempt_url)
            # Intercept API responses for Reddit
            api_responses: list[str] = []
            if "reddit.com" in attempt_url:
                page.on("response", lambda resp: (
                    lambda url: (api_responses.append(url) if "graphql" in url or "/comments.json" in url else None)
                )(resp.url))

            await page.goto(attempt_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            # For Reddit, wait longer for JS-rendered content
            if "reddit.com" in attempt_url and "m.reddit" not in attempt_url:
                try:
                    await page.wait_for_load_state("networkidle", timeout=timeout_s * 500)
                except Exception:
                    pass
            else:
                # Best-effort settle; plenty of sites never hit networkidle.
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            # Scroll to trigger lazy-loaded content (especially for Reddit comments on www)
            if "reddit.com" in attempt_url and "m.reddit" not in attempt_url:
                log.info("scrolling reddit page to load lazy content")
                # Click 'Load more comments' buttons and scroll
                try:
                    await page.evaluate("""
                        (() => {
                            let loaded = 0;
                            // Keep clicking 'load more' buttons until none remain
                            for (let i = 0; i < 10; i++) {
                                const selectors = [
                                    '[data-testid="comment-ui"] [class*="moreComments"]',
                                    '[class*="LoadMore"]',
                                    '[class*="load-more"]',
                                    'button[class*="more"]',
                                    '[aria-label*="load more"]',
                                ];
                                let found = false;
                                selectors.forEach(sel => {
                                    document.querySelectorAll(sel).forEach(btn => {
                                        if (btn.offsetParent !== null) { // only visible
                                            btn.click();
                                            found = true;
                                            loaded++;
                                        }
                                    });
                                });
                                if (!found) break;
                            }
                            return loaded;
                        })()
                    """)
                except Exception:
                    pass
                # Scroll through the page multiple times
                for cycle in range(5):
                    await _scroll_page(page, scroll_count=15, delay_ms=300)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
            html = await page.content()
            if html and len(html) > 1000:
                log.info("scrape succeeded via %s (%d bytes, %d api calls intercepted)", attempt_url, len(html), len(api_responses))
                # Extract Reddit comments while page is still open
                if "reddit.com" in attempt_url:
                    reddit_comments_html = await _extract_reddit_comments(page)
                    log.info("reddit comments extracted: %d chars", len(reddit_comments_html or ""))
                break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning("scrape attempt failed: %s -> %s", attempt_url, last_error)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    if html is None or len(html) < 1000:
        return {"url": url, "status": "error", "error": f"navigation failed: {last_error}"}

    try:
        content = (
            trafilatura.extract(
                html,
                output_format="markdown",
                url=url,
                include_comments=True,
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

    # For Reddit URLs, append DOM-extracted comments that trafilatura missed
    if "reddit.com" in url and reddit_comments_html:
        content = f"{content}\n\n--- Reddit Comments (DOM Extracted) ---\n\n{reddit_comments_html}"

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
        context = await _make_context(browser)
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


async def download_file(url: str, workspace_dir: str) -> dict:
    """Download a file from URL into the workspace directory.

    Returns metadata about the downloaded file.
    """
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    # Derive filename from URL path
    parsed = url.split("?")[0]  # strip query params
    fname = parsed.split("/")[-1]
    if not fname or fname.endswith("/"):
        fname = "downloaded_file"

    dest = ws / fname
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            data = r.content
            dest.write_bytes(data)
            size = len(data)
            # Guess content type from extension
            ext = Path(fname).suffix.lstrip(".").lower()
            mime_map = {
                "txt": "text/plain",
                "md": "text/markdown",
                "py": "text/x-python",
                "json": "application/json",
                "yaml": "text/yaml",
                "yml": "text/yaml",
                "csv": "text/csv",
                "html": "text/html",
                "xml": "text/xml",
                "js": "text/javascript",
                "ts": "text/typescript",
                "css": "text/css",
            }
            mime = mime_map.get(ext, r.headers.get("Content-Type", "application/octet-stream"))
            return {
                "status": "ok",
                "path": str(dest),
                "filename": fname,
                "size": size,
                "mime_type": mime,
            }
    except Exception as e:
        log.warning("download failed: %s -> %s", url, e)
        return {"status": "error", "url": url, "error": f"{type(e).__name__}: {e}"}


def clone_repo(git_url: str, workspace_dir: str) -> dict:
    """Clone a git repository into the workspace directory.

    Returns metadata about the cloned repo.
    """
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    # Derive repo name from URL
    repo_name = git_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    dest = ws / repo_name
    try:
        result = subprocess.run(
            ["git", "clone", git_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or f"exit {result.returncode}")[:500]
            return {"status": "error", "git_url": git_url, "error": err_msg}

        # Count files in the repo
        file_count = sum(1 for _ in dest.rglob("*") if _.is_file())
        return {
            "status": "ok",
            "path": str(dest),
            "repo_name": repo_name,
            "file_count": file_count,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "git_url": git_url, "error": "Clone timed out (120s)"}
    except Exception as e:
        log.warning("clone failed: %s -> %s", git_url, e)
        return {"status": "error", "git_url": git_url, "error": f"{type(e).__name__}: {e}"}


def list_files(path: str, workspace_dir: str) -> dict:
    """List files recursively in a path within the workspace directory.

    Returns a tree-like listing of files and directories.
    """
    ws = Path(workspace_dir)
    target = (ws / path).resolve()

    if not target.is_dir():
        return {"status": "error", "path": path, "error": f"Not a directory: {target}"}

    entries = []
    for item in sorted(target.rglob("*")):
        rel = item.relative_to(ws)
        if item.is_dir():
            entries.append({"name": str(rel), "type": "dir", "size": 0})
        else:
            entries.append({"name": str(rel), "type": "file", "size": item.stat().st_size})

    return {
        "status": "ok",
        "path": path,
        "total_entries": len(entries),
        "entries": entries[:500],  # cap at 500 to avoid huge responses
    }


def cat_file(path: str, workspace_dir: str) -> dict:
    """Read and return the text content of a file in the workspace directory."""
    ws = Path(workspace_dir)
    target = (ws / path).resolve()

    if not target.is_file():
        return {"status": "error", "path": path, "error": f"Not a file: {target}"}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "status": "ok",
            "path": str(target),
            "filename": target.name,
            "size": len(content.encode("utf-8")),
            "content": content[:100_000],  # cap at ~100k chars
        }
    except Exception as e:
        log.warning("cat failed: %s -> %s", path, e)
        return {"status": "error", "path": path, "error": f"{type(e).__name__}: {e}"}
