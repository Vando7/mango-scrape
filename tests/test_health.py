"""Health endpoint tests."""

import pytest


async def test_health_ok(http_client):
    """GET /health should return status ok."""
    r = await http_client.get("/health")
    assert r.status_code == 200, f"Unexpected: {r.text}"
    data = r.json()
    assert data["status"] == "ok"
