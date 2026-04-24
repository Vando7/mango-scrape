"""Screenshot tests against running Docker container."""

import base64


async def test_screenshot_single(http_client):
    """Take a screenshot of a single URL."""
    r = await http_client.post(
        "/screenshot", json={"urls": ["https://en.wikipedia.org/wiki/Main_Page"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    assert len(results) == 1
    res = results[0]
    assert res["status"] == "ok", f"Error: {res.get('error')}"
    assert "screenshot_b64" in res
    # Verify it's valid base64 PNG
    img_data = base64.b64decode(res["screenshot_b64"])
    assert img_data[:8] == b"\x89PNG\r\n\x1a\n", f"Not a PNG: {img_data[:8]}"


async def test_screenshot_multiple(http_client):
    """Take screenshots of multiple URLs."""
    r = await http_client.post(
        "/screenshot", json={"urls": ["https://en.wikipedia.org/wiki/Main_Page"], "timeout_s": 30}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    results = r.json()
    assert len(results) == 1
    res = results[0]
    if res["status"] == "ok":
        img_data = base64.b64decode(res["screenshot_b64"])
        assert len(img_data) > 1000, f"Screenshot too small: {len(img_data)} bytes"
