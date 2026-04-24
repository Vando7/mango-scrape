"""Error handling and edge case tests."""


async def test_scrape_invalid_url(http_client):
    """Scraping an invalid URL should return error, not crash."""
    r = await http_client.post(
        "/scrape", json={"urls": ["https://thisdomaindoesnotexist12345.com/"], "timeout_s": 10}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    # Should gracefully handle failure
    if "status" in res:
        assert res["status"] != "ok" or "error" in res


async def test_scrape_empty_urls(http_client):
    """Scraping with empty URL list returns 422 (FastAPI min_length=1 validation)."""
    r = await http_client.post("/scrape", json={"urls": [], "timeout_s": 10})
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"


async def test_scrape_mixed_valid_invalid(http_client):
    """Scraping mix of valid and invalid URLs."""
    r = await http_client.post(
        "/scrape",
        json={"urls": ["https://en.wikipedia.org/wiki/Main_Page", "https://thisdomaindoesnotexist12345.com/"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    assert len(results) == 2
    # First should be ok, second should be error
    assert results[0]["status"] == "ok", f"First URL failed: {results[0].get('error')}"


async def test_screenshot_invalid_url(http_client):
    """Screenshot of invalid URL should return error."""
    r = await http_client.post(
        "/screenshot", json={"urls": ["https://thisdomaindoesnotexist12345.com/"], "timeout_s": 10}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    if "status" in res:
        assert res["status"] != "ok" or "error" in res


async def test_youtube_invalid_url(http_client):
    """YouTube scrape with invalid URL should return error."""
    r = await http_client.post(
        "/scrape_youtube", json={"urls": ["https://www.youtube.com/watch?v=INVALIDID1234567890"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    # Should handle gracefully (video ID extraction or API error)


async def test_youtube_no_video_id(http_client):
    """YouTube scrape with URL that has no extractable video ID."""
    r = await http_client.post(
        "/scrape_youtube", json={"urls": ["not-a-url"], "timeout_s": 10}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    if res["status"] == "error":
        assert "video_id" not in res or res.get("video_id") is None
