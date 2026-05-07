"""Patchright + trafilatura: stealth browser -> clean markdown."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from patchright.async_api import Browser, async_playwright

from yt_transcript import get_page, paginate_transcript

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


async def _scroll_page(
    page, scroll_count: int = 10, delay_ms: int = 500, scroll_px: int = 1500
):
    """Scroll down the page multiple times to trigger lazy-loaded content."""
    for i in range(scroll_count):
        try:
            await page.evaluate(f"window.scrollBy(0, {scroll_px})")
            await asyncio.sleep(delay_ms / 1000)
        except Exception:
            break


MAX_LINKS = 10


async def _extract_links(page) -> list[dict]:
    """Extract meaningful links from the page, capped at MAX_LINKS.

    Prioritises links with longer, more descriptive anchor text so the model
    can understand what each link points to without needing to scrape it.
    """
    try:
        links = await page.evaluate("""
            (() => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    const text = (a.innerText || '').trim();
                    const url = a.href;
                    if (!text || !url || !url.startsWith('http')) return;
                    const key = url.split('#')[0];
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({ text: text.substring(0, 200), url: url });
                });
                return results;
            })()
        """)
        # Filter: skip short URLs, navigation keywords, and single-word links
        nav_words = {
            "back",
            "next",
            "prev",
            "first",
            "last",
            "read more",
            "continue",
            "see more",
            "more",
            "click here",
            "learn more",
            "new",
            "past",
            "ask",
            "show",
            "jobs",
            "submit",
            "login",
            "logout",
            "about",
            "contact",
            "help",
            "search",
            "menu",
            "toggle",
            "settings",
            "hide",
            "discuss",
            "comments",
            "profile",
            "share",
            "save",
            "report",
            "reply",
            "permalink",
            "embed",
            "source",
            "home",
            "top",
            "best",
        }
        out = []
        for item in links or []:
            t = item["text"].lower().strip()
            url = item["url"]
            if len(url) < 20:
                continue
            if t in nav_words or len(t.split()) < 2:
                continue  # skip single-word and nav links
            out.append(item)
        # Sort by anchor text length (desc) so the most descriptive links win
        out.sort(key=lambda x: len(x["text"]), reverse=True)
        return out[:MAX_LINKS]
    except Exception as e:
        log.warning("link extraction failed: %s", e)
        return []


async def _extract_reddit_comments(page) -> str:
    """Extract all visible comment text from a Reddit page via JS selectors."""
    try:
        comments = await page.evaluate("""
            (() => {
                const results = [];
                // Extract post body from .md-block or .usertext
                const postSelectors = [
                    '.md-block',
                    '.usertext-body',
                    '[class*="md-block"]',
                    '[class*="body__"]',
                ];
                for (const sel of postSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim().length > 30) {
                        results.push('--- POST ---\n' + el.innerText.trim());
                        break;
                    }
                }
                // Extract comments from Reddit's comment UI
                const commentSelectors = [
                    '[data-testid="comment-ui"]',
                    '[class*="CommentBlock"]',
                    '[class*="comment"]',
                    '[class*="Comment"]',
                    '[class*="ElasticBlock"]',
                ];
                for (const sel of commentSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 15) {
                            // Skip UI chrome like "Reply", "Share", "Sort",
                            // "Add comment", "Post", "New", etc.
                            const lower = text.toLowerCase();
                            if (lower.includes('add comment') || lower.includes('sort by') ||
                                lower.includes('reply') || lower.includes('share') ||
                                lower.includes('post') || lower.includes('new comment')) {
                                return;
                            }
                            let isDuplicate = false;
                            for (const r of results) {
                                if (r.startsWith(text.substring(0, 30))) { isDuplicate = true; break; }
                            }
                            if (!isDuplicate) {
                                results.push(text);
                            }
                        }
                    });
                }
                return results.join('\n\n---\n\n');
            })()
        """)
        return comments or ""
    except Exception as e:
        log.warning("reddit comment extraction failed: %s", e)
        return ""


def _compact(text: str) -> str:
    """Collapse whitespace/newlines so JSON-escaping doesn't eat tokens."""
    if not text:
        return text
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:youtube\.com/.*v=)([\w-]{11})",
        r"(?:youtu\.be/)([\w-]{11})",
        r"(?:youtube\.com/embed/)([\w-]{11})",
        r"(?:youtube\.com/v/)([\w-]{11})",
        r"(?:youtube\.com/shorts/)([\w-]{11})",
        r"(?:youtube\.com/watch[^&]*&v=)([\w-]{11})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    # Also try plain 11-char ID
    m = re.search(r"([\w-]{11})", url)
    if m and len(m.group(1)) == 11:
        return m.group(1)
    return None


async def _scrape_youtube_comments(browser: Browser, url: str, timeout_s: int) -> dict:
    """Scrape YouTube comments + metadata using Playwright."""
    context = None
    try:
        context = await _make_context(browser)
        page = await context.new_page()
        log.info("scraping youtube from %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        # Wait for comments to load
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        # Scroll to trigger lazy-loaded comments
        await _scroll_page(page, scroll_count=20, delay_ms=300)
        await asyncio.sleep(1)

        # Extract metadata from page (title, channel, date, description)
        meta = await page.evaluate("""
            (() => {
                const result = {};
                // Video title from <title> tag
                result.title = document.querySelector('title')?.innerText || '';
                // Clean up YouTube title format: "Video Title - Channel | YouTube"
                if (result.title.includes('|')) {
                    result.title = result.title.split('|')[0].trim();
                }
                // Meta description
                const descMeta = document.querySelector('meta[name="description"]');
                result.description = descMeta?.content || '';
                // Try to find structured data in og: meta tags
                const ogTags = document.querySelectorAll('meta[property]');
                for (const tag of ogTags) {
                    const prop = tag.getAttribute('property');
                    if (prop === 'og:title' && !result.title) result.title = tag.content;
                    if (prop === 'og:description') result.description = tag.content;
                    if (prop === 'og:video:uploader') result.channel = tag.content;
                }
                // Try JSON-LD
                try {
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        if (!s.textContent || !s.textContent.includes('@context')) continue;
                        try {
                            const data = JSON.parse(s.textContent);
                            if (data?.name && data['@context']) {
                                result.channel = data.author?.name || '';
                                result.upload_date = data.datePublished || data.uploadDate || '';
                                break;
                            }
                        } catch(e) {}
                    }
                } catch(e) {}

                // Fallback: try to get channel from page elements
                if (!result.channel) {
                    const ytChannel = document.querySelector('#channel-name a[href]');
                    if (ytChannel) result.channel = ytChannel.textContent?.trim();
                }
                if (!result.channel) {
                    const ytMeta = document.querySelector('meta[name="og:video:creator"]');
                    if (ytMeta) result.channel = ytMeta.content;
                }

                return JSON.stringify(result);
            })()
        """)

        # Extract comments from DOM
        try:
            comments = await page.evaluate("""
                (() => {
                    const results = [];
                    // YouTube comment structure: [data-item-id] or [jscontent="dropdownButton"]
                    document.querySelectorAll('[data-item-id], [class*="comment-text"]').forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 10) {
                            results.push(text);
                        }
                    });
                    // Also try class-based selectors
                    document.querySelectorAll('[class*="comment-body"], [class*="comment-text-wrapper"]').forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 10) {
                            let isDup = results.some(r => r.startsWith(text.substring(0, 30)));
                            if (!isDup) results.push(text);
                        }
                    });
                    return results;
                })()
            """)
        except Exception:
            comments = []

        # Deduplicate
        seen = set()
        unique_comments = []
        for c in comments or []:
            if c not in seen and len(c) > 10:
                seen.add(c)
                unique_comments.append(c)

        # Parse metadata JSON string
        video_meta = {}
        try:
            video_meta = json.loads(meta)
        except Exception:
            pass

        # Compact whitespace so JSON escaping doesn't waste tokens
        for k in ("title", "description"):
            if isinstance(video_meta.get(k), str):
                video_meta[k] = _compact(video_meta[k])

        return {
            "status": "ok",
            "url": url,
            "comment_count": len(unique_comments),
            "comments": [_compact(c) for c in unique_comments[:200]],
            "video_title": video_meta.get("title", ""),
            "channel": video_meta.get("channel", ""),
            "upload_date": video_meta.get("upload_date", ""),
            "description": _compact(video_meta.get("description", "")),
        }
    except Exception as e:
        log.warning("youtube comment scrape failed: %s", e)
        return {
            "status": "error",
            "url": url,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def scrape_youtube(
    browser: Browser, url: str, timeout_s: int, language: str = "en"
) -> dict:
    """Scrape a YouTube video: transcript + comments.

    Args:
        browser: Patchright browser instance.
        url: YouTube video URL.
        timeout_s: Timeout in seconds.
        language: Language code(s) for transcript. Default 'en'.
                  Supports single codes ('bg') or comma-separated ('en,bg').
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return {
            "status": "error",
            "url": url,
            "error": "Could not extract video ID from URL",
        }

    # Fetch paginated transcript (cached, no browser needed)
    transcript_result = paginate_transcript(video_id, language)

    # Scrape comments (needs browser)
    comment_result = await _scrape_youtube_comments(browser, url, timeout_s)

    # Combine results — send first page + metadata so model can request more
    content_parts = []
    if transcript_result.get("status") == "ok":
        total_pages = transcript_result.get("total_pages", 1)
        cached = transcript_result.get("cached", False)
        meta_tag = " [cached]" if cached else ""
        page_info = f"\n---\nTranscript: {total_pages} page(s){meta_tag}. Page 1 of {total_pages}. Say 'next' for more pages."
        first_page = (
            transcript_result.get("pages", [""])[0]
            if transcript_result.get("pages")
            else ""
        )
        content_parts.append(f"# Transcript{page_info}\n\n{first_page}")
    else:
        content_parts.append(
            f"# Transcript Error: {transcript_result.get('error', 'unknown')}"
        )

    if comment_result.get("status") == "ok":
        comments_text = "\n\n---\n\n".join(
            comment_result["comments"][:50]
        )  # top 50 for content
        content_parts.append(
            f"\n# Comments ({comment_result['comment_count']} total)\n\n{comments_text}"
        )
    else:
        content_parts.append(
            f"\n# Comments Error: {comment_result.get('error', 'unknown')}"
        )

    full_content = "\n\n".join(content_parts)

    return {
        "url": url,
        "status": "ok",
        "video_id": video_id,
        "title": _compact(comment_result.get("video_title", f"YouTube: {video_id}")),
        "channel": comment_result.get("channel", ""),
        "upload_date": comment_result.get("upload_date", ""),
        "description": _compact(comment_result.get("description", "")),
        "markdown_content": _compact(full_content),
        "word_count": len(full_content.split()) if full_content else 0,
        "transcript_status": transcript_result.get("status", "error"),
        "comment_count": comment_result.get("comment_count", 0)
        if comment_result.get("status") == "ok"
        else 0,
    }


async def _make_context(browser: Browser, is_reddit: bool = False) -> Any:
    """Create a stealthy browser context with realistic fingerprints."""
    extra_headers = None
    if is_reddit:
        extra_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1920, "height": 1080},
        extra_http_headers=extra_headers,
    )
    return ctx


async def _inject_stealth(page) -> None:
    """Inject stealth scripts to bypass bot detection (old.reddit.com etc)."""
    # Run before navigation so properties are set from the start
    await page.add_init_script("""
        // Override navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        // Override chrome runtime (missing in headless)
        window.chrome = { runtime: {} };

        // Override plugins
        const querySelector = Document.prototype.querySelector;
        Document.prototype.querySelector = function(q) {
            const result = querySelector.apply(this, arguments);
            if (result && q.includes('script[src*="recaptcha"]')) {
                return null;  // Block recaptcha detection
            }
            return result;
        };

        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        // Override plugins and mimeTypes
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugin = function() { return undefined; };
                const search = function(query) { return null; };
                plugin.toString = function() { return "[object Plugin]"; };
                const plugins = new Proxy({}, {
                    get: (target, prop) => search(prop),
                    length: 0,
                    [Symbol.iterator]: () => ({ next: () => ({ done: true }) }),
                });
                return Object.setPrototypeOf(plugins, PluginArray.prototype);
            },
        });

        // Override languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });

        // Override hardware concurrency (common bot detection vector)
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 4,
        });
    """)


async def scrape_one(browser: Browser, url: str, timeout_s: int) -> dict:
    """Scrape one URL. Always returns a dict — errors are captured, never raised.

    For Reddit URLs, tries old.reddit first (lighter, easier to parse), then www, then m.reddit.
    """
    # Reddit-specific fallback chain: www.reddit first (SSR post body),
    # then old.reddit (cleaner HTML, no JS), then m.reddit.
    urls_to_try = [url]
    if (
        "reddit.com" in url
        and "old.reddit.com" not in url
        and "m.reddit.com" not in url
    ):
        base = url.split("?")[0].rstrip("/")
        # Extract path after domain (e.g. /r/python)
        parts = base.split("/", 3)  # ["https:", "", "www.reddit.com", "/r/python"]
        path = f"/{parts[3]}" if len(parts) > 3 else ""
        urls_to_try = [
            url,  # www.reddit first (SSR post body)
            f"https://old.reddit.com{path}",
            f"https://m.reddit.com{path}",
        ]

    context: Any | None = None
    html: str | None = None
    last_error: str | None = None
    reddit_comments_html: str | None = None
    page_links: list[dict] = []

    for attempt_url in urls_to_try:
        is_reddit = "reddit.com" in attempt_url
        context = await _make_context(browser, is_reddit=is_reddit)
        page = await context.new_page()
        try:
            log.info("scrape attempt: %s", attempt_url)
            # Inject stealth scripts for bot detection bypass (must run before navigation)
            if is_reddit:
                await _inject_stealth(page)

            await page.goto(
                attempt_url, wait_until="domcontentloaded", timeout=timeout_s * 1000
            )
            # For Reddit, wait longer for JS-rendered content
            if "reddit.com" in attempt_url and "m.reddit" not in attempt_url:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=timeout_s * 500
                    )
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
                                    '[data-click-handler="load-more-comments"]',
                                    '[class*="LoadMoreComments"]',
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
            # For Reddit, check if post body actually rendered (old.reddit often
            # just has the shell with no post/comments)
            if "reddit.com" in attempt_url and "m.reddit" not in attempt_url:
                has_content = await page.evaluate("""
                    (() => {
                        const selectors = [
                            '[class*="md-block"]',
                            '[class*="usertext-body"]',
                            '[class*="comment"]',
                            '[data-testid="comment-ui"]',
                        ];
                        for (const sel of selectors) {
                            if (document.querySelector(sel)) return true;
                        }
                        return false;
                    })()
                """)
                if not has_content:
                    log.warning(
                        "reddit page %s has no post body — trying next URL",
                        attempt_url,
                    )
                    continue
            if html and len(html) > 1000:
                log.info("scrape succeeded via %s (%d bytes)", attempt_url, len(html))
                # Extract Reddit comments while page is still open
                if "reddit.com" in attempt_url:
                    reddit_comments_html = await _extract_reddit_comments(page)
                    log.info(
                        "reddit comments extracted: %d chars",
                        len(reddit_comments_html or ""),
                    )
                # Extract links for general-purpose browsing
                page_links = await _extract_links(page)
                log.info("extracted %d links", len(page_links))
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
        return {
            "url": url,
            "status": "error",
            "error": f"navigation failed: {last_error}",
        }

    try:
        raw_content = (
            trafilatura.extract(
                html,
                output_format="markdown",
                url=url,
                include_comments=True,
                include_tables=True,
            )
            or ""
        )
        content = _compact(raw_content)
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
        content = f"{content}\n\n--- Reddit Comments (DOM Extracted) ---\n\n{_compact(reddit_comments_html)}"
    content = _compact(content)

    links_flat = "\n".join(f"{l['text']} | {l['url']}" for l in page_links)
    return {
        "url": url,
        "status": "ok",
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "date": getattr(meta, "date", None),
        "description": _compact(getattr(meta, "description", None) or ""),
        "markdown_content": content,
        "word_count": len(content.split()) if content else 0,
        "links": links_flat,
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
    ws_resolved = ws.resolve()

    # Derive filename from URL path
    parsed = url.split("?")[0]  # strip query params
    fname = parsed.split("/")[-1]
    if not fname or fname.endswith("/"):
        fname = "downloaded_file"

    dest = (ws / fname).resolve()
    if not dest.is_relative_to(ws_resolved):
        return {"status": "error", "url": url, "error": "filename escapes workspace"}
    max_mb = int(os.environ.get("DEEP_DIVE_MAX_DOWNLOAD_MB", "100"))
    max_bytes = max_mb * 1024 * 1024
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", url, follow_redirects=True) as r:
                r.raise_for_status()
                size = 0
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            f.close()
                            try:
                                dest.unlink()
                            except FileNotFoundError:
                                pass
                            return {
                                "status": "error",
                                "url": url,
                                "error": f"exceeds max size ({max_mb} MB)",
                            }
                        f.write(chunk)
                content_type = r.headers.get("Content-Type", "application/octet-stream")
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
            mime = mime_map.get(ext, content_type)
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
            ["git", "clone", "--", git_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or f"exit {result.returncode}")[
                :500
            ]
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
        return {
            "status": "error",
            "git_url": git_url,
            "error": "Clone timed out (120s)",
        }
    except Exception as e:
        log.warning("clone failed: %s -> %s", git_url, e)
        return {
            "status": "error",
            "git_url": git_url,
            "error": f"{type(e).__name__}: {e}",
        }


def list_files(path: str, workspace_dir: str) -> dict:
    """List files recursively in a path within the workspace directory.

    Returns a tree-like listing of files and directories.
    """
    ws = Path(workspace_dir)
    ws_resolved = ws.resolve()
    target = (ws / path).resolve()

    if not target.is_relative_to(ws_resolved):
        return {"status": "error", "path": path, "error": "path escapes workspace"}

    if not target.is_dir():
        return {"status": "error", "path": path, "error": f"Not a directory: {target}"}

    entries = []
    for item in sorted(target.rglob("*")):
        rel = item.relative_to(ws)
        if item.is_dir():
            entries.append({"name": str(rel), "type": "dir", "size": 0})
        else:
            entries.append(
                {"name": str(rel), "type": "file", "size": item.stat().st_size}
            )

    return {
        "status": "ok",
        "path": path,
        "total_entries": len(entries),
        "entries": entries[:500],  # cap at 500 to avoid huge responses
    }


def cat_file(path: str, workspace_dir: str) -> dict:
    """Read and return the text content of a file in the workspace directory."""
    ws = Path(workspace_dir)
    ws_resolved = ws.resolve()
    target = (ws / path).resolve()

    if not target.is_relative_to(ws_resolved):
        return {"status": "error", "path": path, "error": "path escapes workspace"}

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


async def hn_search(
    query: str,
    num_results: int = 10,
    tags: str = "story",
    sort_by_date: bool = False,
) -> dict:
    """Search Hacker News via the Algolia API. No auth, free."""
    base = (
        "https://hn.algolia.com/api/v1/search_by_date"
        if sort_by_date
        else "https://hn.algolia.com/api/v1/search"
    )
    params = {
        "query": query,
        "tags": tags,
        "hitsPerPage": min(num_results, 50),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(base, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("hn search failed: %s -> %s", query, e)
        return {"status": "error", "query": query, "error": f"{type(e).__name__}: {e}"}

    results = []
    for hit in (data.get("hits") or [])[:num_results]:
        oid = hit.get("objectID", "")
        results.append(
            {
                "title": _compact(hit.get("title") or hit.get("story_title") or ""),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "hn_url": f"https://news.ycombinator.com/item?id={oid}",
                "author": hit.get("author", ""),
                "points": hit.get("points") or 0,
                "num_comments": hit.get("num_comments") or 0,
                "created": hit.get("created_at", ""),
            }
        )
    return {
        "status": "ok",
        "query": query,
        "num_results": len(results),
        "results": results,
    }


async def reddit_search(
    query: str,
    num_results: int = 10,
    subreddit: str = "",
    sort: str = "relevance",
    time: str = "all",
) -> dict:
    """Search Reddit via the public JSON endpoint.

    Reddit rate-limits unauthenticated requests; on 429/403 the caller should
    back off or scope the query to a specific subreddit.
    """
    if subreddit:
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        restrict = "1"
    else:
        url = "https://www.reddit.com/search.json"
        restrict = "0"
    params = {
        "q": query,
        "limit": min(num_results, 25),
        "sort": sort,
        "t": time,
        "restrict_sr": restrict,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("reddit search failed: %s -> %s", query, e)
        return {"status": "error", "query": query, "error": f"{type(e).__name__}: {e}"}

    results = []
    for item in (data.get("data", {}).get("children") or [])[:num_results]:
        d = item.get("data", {})
        permalink = f"https://www.reddit.com{d.get('permalink', '')}"
        selftext = (d.get("selftext") or "")[:500]
        results.append(
            {
                "title": _compact(d.get("title", "")),
                "selftext_preview": _compact(selftext),
                "url": d.get("url", "") or permalink,
                "permalink": permalink,
                "subreddit": d.get("subreddit", ""),
                "author": d.get("author", ""),
                "score": d.get("score") or 0,
                "num_comments": d.get("num_comments") or 0,
                "created": d.get("created_utc", 0),
            }
        )
    return {
        "status": "ok",
        "query": query,
        "num_results": len(results),
        "results": results,
    }
