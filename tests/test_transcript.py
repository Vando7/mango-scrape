"""Transcript pagination and SQLite cache tests."""


async def test_transcript_page_first(http_client):
    """Fetch page 1 of a known video — should succeed with metadata."""
    r = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 1, "language": "en"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["total_pages"] > 1
    assert d["page_num"] == 1
    assert len(d["transcript"]) > 50


async def test_transcript_page_middle(http_client):
    """Fetch a middle page — should succeed."""
    r = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 12, "language": "en"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["page_num"] == 12


async def test_transcript_page_last(http_client):
    """Fetch the last page — should succeed."""
    # First get total_pages from page 1
    r1 = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 1, "language": "en"},
    )
    assert r1.status_code == 200
    total = r1.json()["total_pages"]

    # Fetch last page
    r2 = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": total, "language": "en"},
    )
    assert r2.status_code == 200
    d = r2.json()
    assert d["status"] == "ok"
    assert d["page_num"] == total


async def test_transcript_page_out_of_range(http_client):
    """Page beyond total_pages should return error."""
    r = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 999, "language": "en"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "error"


async def test_transcript_page_zero(http_client):
    """Page 0 should return error."""
    r = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 0, "language": "en"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "error"


async def test_transcript_page_cached(http_client):
    """Second request for same video should hit cache."""
    # First call (already cached from earlier tests)
    r1 = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 1, "language": "en"},
    )
    assert r1.status_code == 200
    d1 = r1.json()

    # Second call — should be cached
    r2 = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 1, "language": "en"},
    )
    assert r2.status_code == 200
    d2 = r2.json()

    # Both should be ok and cached
    assert d2["cached"] is True


async def test_transcript_page_custom_size(http_client):
    """Custom page size should change total_pages."""
    r = await http_client.post(
        "/transcript_page",
        json={"video_id": "_U6rQcCIppc", "page_num": 1, "language": "en", "page_size": 500},
    )
    assert r.status_code == 200
    d = r.json()
    # 500-word pages should produce more pages than default 2000
    assert d["total_pages"] > 10
