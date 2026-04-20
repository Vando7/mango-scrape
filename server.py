"""FastAPI service wrapping the scraper over HTTP.

Runs inside the Docker container. The MCP shim on Windows talks to it.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from scraper import launch_browser, scrape_many, screenshot_many

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("deep-dive")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("launching patchright browser")
    app.state.browser, app.state.pw = await launch_browser()
    try:
        yield
    finally:
        log.info("closing patchright browser")
        try:
            await app.state.browser.close()
        finally:
            await app.state.pw.stop()


app = FastAPI(title="deep-dive scraper", lifespan=lifespan)


class ScrapeRequest(BaseModel):
    urls: list[str] = Field(min_length=1)
    timeout_s: int = 30


@app.post("/scrape")
async def scrape(req: ScrapeRequest) -> list[dict]:
    log.info("scrape %d url(s)", len(req.urls))
    return await scrape_many(app.state.browser, req.urls, req.timeout_s)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/screenshot")
async def screenshot(req: ScrapeRequest) -> list[dict]:
    log.info("screenshot %d url(s)", len(req.urls))
    return await screenshot_many(app.state.browser, req.urls, req.timeout_s)
