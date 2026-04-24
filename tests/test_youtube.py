"""YouTube scraping tests against running Docker container."""


# Test URLs — real YouTube videos with transcripts available
YOUTUBE_URLS = [
    "https://www.youtube.com/watch?v=jNQX49jZOzQ",  # OK Go - Here I Go (classic viral video)
]


async def test_youtube_scrape(http_client):
    """Scrape a YouTube video: should return transcript + comments."""
    r = await http_client.post(
        "/scrape_youtube", json={"urls": YOUTUBE_URLS[:1], "timeout_s": 60}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]

    # Check structure regardless of success/failure
    assert "url" in res
    assert "status" in res
    assert "video_id" in res or "error" in res

    if res["status"] == "ok":
        assert res["title"] is not None and len(res["title"]) > 5
        assert isinstance(res.get("comment_count"), int)
        # Transcript status should be present
        assert "transcript_status" in res


async def test_youtube_returns_video_id(http_client):
    """Verify video ID extraction works."""
    r = await http_client.post(
        "/scrape_youtube", json={"urls": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], "timeout_s": 60}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    if res["status"] == "ok":
        # video_id should be exactly 11 chars (YouTube ID format)
        assert len(res["video_id"]) == 11


async def test_youtube_shorts_url(http_client):
    """Test YouTube Shorts URL format."""
    r = await http_client.post(
        "/scrape_youtube", json={"urls": ["https://www.youtube.com/shorts/dQw4w9WgXcQ"], "timeout_s": 60}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    res = results[0]
    # Should extract video ID even from shorts URL
    if res["status"] == "ok":
        assert len(res["video_id"]) == 11
