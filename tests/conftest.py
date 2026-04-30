"""Shared test fixtures for HTTP API tests against the running Docker container."""

from __future__ import annotations

import os

import httpx
import pytest


# Server URL — defaults to localhost:9161 (Docker container)
SERVER_URL = os.environ.get("SCRAPER_SERVER_URL", "http://localhost:9161")

@pytest.fixture
async def http_client():
    """Provide a shared httpx.AsyncClient."""
    async with httpx.AsyncClient(base_url=SERVER_URL, timeout=60) as client:
        yield client


def _scrape_payload(urls: list[str], timeout_s: int = 30) -> dict:
    return {"urls": urls, "timeout_s": timeout_s}
