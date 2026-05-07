"""Scraping tests against a running scraper server."""

from __future__ import annotations

import pytest


# Test URLs — mix of article sites, docs pages, etc.
ARTICLE_URLS = [
    "https://www.reuters.com/",  # news site with heavy JS
    "https://en.wikipedia.org/wiki/Main_Page",  # Wikipedia (simple HTML)
    "https://blog.python.org/",  # Python blog
]

REDDIT_URLS = [
    "https://old.reddit.com/r/python/",  # old Reddit
    "https://www.reddit.com/r/Python/",  # www Reddit
]


async def test_health_endpoint(http_client):
    """Sanity: server is reachable."""
    r = await http_client.get("/health")
    assert r.status_code == 200


async def test_scrape_single_article(http_client):
    """Scrape a single article URL and verify structure."""
    r = await http_client.post(
        "/scrape", json={"urls": ["https://en.wikipedia.org/wiki/Main_Page"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    assert len(results) == 1
    res = results[0]
    assert res["status"] == "ok", f"Error: {res.get('error')}"
    assert res["url"] == "https://en.wikipedia.org/wiki/Main_Page"
    assert res["title"] is not None and len(res["title"]) > 5
    assert res["markdown_content"] is not None and len(res["markdown_content"]) > 100
    assert res["word_count"] > 0


async def test_scrape_multiple_articles(http_client):
    """Scrape multiple URLs at once."""
    r = await http_client.post(
        "/scrape", json={"urls": ARTICLE_URLS[:2], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    assert len(results) == 2
    for res in results:
        assert "url" in res
        assert "status" in res
        if res["status"] == "ok":
            assert "markdown_content" in res
            assert "word_count" in res


async def test_scrape_reddit_old(http_client):
    """Scrape old.reddit.com — should return content + comments."""
    r = await http_client.post(
        "/scrape", json={"urls": ["https://old.reddit.com/r/python/"], "timeout_s": 45}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    # Reddit may or may not succeed depending on bot detection; just check structure
    assert "status" in res
    if res["status"] == "ok":
        assert "markdown_content" in res


async def test_scrape_reddit_www(http_client):
    """Scrape www.reddit.com — should also work."""
    r = await http_client.post(
        "/scrape", json={"urls": ["https://www.reddit.com/r/Python/"], "timeout_s": 45}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    assert "status" in res


async def test_scrape_returns_word_count(http_client):
    """Verify word_count is a positive integer for successful scrapes."""
    r = await http_client.post(
        "/scrape", json={"urls": ["https://en.wikipedia.org/wiki/Python_(programming_language)"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    if res["status"] == "ok":
        assert isinstance(res["word_count"], int)
        assert res["word_count"] > 0
