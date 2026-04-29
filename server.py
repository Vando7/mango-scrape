"""FastAPI service wrapping the scraper over HTTP.

Runs inside the Docker container. The MCP shim on Windows talks to it.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from scraper import (
    cat_file,
    clone_repo,
    download_file,
    launch_browser,
    list_files,
    scrape_many,
    scrape_youtube,
    screenshot_many,
)

from yt_transcript import get_page

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
    language: str = "en"


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


@app.post("/scrape_youtube")
async def scrape_youtube_endpoint(req: ScrapeRequest) -> list[dict]:
    log.info("scrape_youtube %d url(s), language=%s", len(req.urls), req.language)
    results = []
    for u in req.urls:
        r = await scrape_youtube(app.state.browser, u, req.timeout_s, req.language)
        results.append(r)
    return results


class TranscriptPageRequest(BaseModel):
    video_id: str = Field(min_length=1)
    language: str = "en"
    page_num: int = 1
    page_size: int = 5000


@app.post("/transcript_page")
async def transcript_page_endpoint(req: TranscriptPageRequest) -> dict:
    log.info("transcript_page %s, lang=%s, page=%d", req.video_id, req.language, req.page_num)
    return get_page(
        req.video_id,
        language=req.language,
        page_num=req.page_num,
        page_size=req.page_size,
    )


WORKSPACE_DIR = os.environ.get("DEEP_DIVE_WORKSPACE", r"C:\software\searchmcp\scraper\scrape\workspace")


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1)


@app.post("/download")
async def download(req: DownloadRequest) -> dict:
    log.info("download %s", req.url)
    return await download_file(req.url, WORKSPACE_DIR)


class CloneRequest(BaseModel):
    git_url: str = Field(min_length=1)


@app.post("/clone")
async def clone(req: CloneRequest) -> dict:
    log.info("clone %s", req.git_url)
    return clone_repo(req.git_url, WORKSPACE_DIR)


class ListRequest(BaseModel):
    path: str = Field(default=".")


@app.post("/list")
async def list_endpoint(req: ListRequest) -> dict:
    log.info("list %s", req.path)
    return list_files(req.path, WORKSPACE_DIR)


class CatRequest(BaseModel):
    path: str = Field(min_length=1)


@app.post("/cat")
async def cat_endpoint(req: CatRequest) -> dict:
    log.info("cat %s", req.path)
    return cat_file(req.path, WORKSPACE_DIR)
