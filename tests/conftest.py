"""Shared test fixtures for HTTP API tests against a running scraper server."""

from __future__ import annotations

import os

import httpx
import pytest


SERVER_URL = os.environ.get("SCRAPER_SERVER_URL", "http://localhost:8765")

@pytest.fixture
async def http_client():
    """Provide a shared httpx.AsyncClient."""
    async with httpx.AsyncClient(base_url=SERVER_URL, timeout=60) as client:
        yield client


def _scrape_payload(urls: list[str], timeout_s: int = 30) -> dict:
    return {"urls": urls, "timeout_s": timeout_s}
