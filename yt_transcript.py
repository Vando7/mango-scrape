"""YouTube transcript fetching with SQLite cache and word-based pagination."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path

# Lazy import — only needed when actually fetching from YouTube API
YouTubeTranscriptApi = None  # type: ignore

log = logging.getLogger("deep-dive.yt")


# ── SQLite cache ────────────────────────────────────────────────────────

_CACHE_CONN: sqlite3.Connection | None = None


def _reset_db() -> None:
    """Close the cached DB connection (for testing)."""
    global _CACHE_CONN
    if _CACHE_CONN is not None:
        try:
            _CACHE_CONN.close()
        except Exception:
            pass
        _CACHE_CONN = None


def _get_cache_path() -> str:
    """Return the path to the SQLite cache file."""
    return os.environ.get(
        "DEEP_DIVE_CACHE_DB",
        "/cache/yt_transcripts.db",
    )


def _db() -> sqlite3.Connection:
    """Lazy-init a single shared DB connection."""
    global _CACHE_CONN
    if _CACHE_CONN is None:
        db_path = Path(_get_cache_path())
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_CONN = sqlite3.connect(str(db_path))
        _CACHE_CONN.execute("""
            CREATE TABLE IF NOT EXISTS yt_transcripts (
                video_id TEXT,
                language TEXT,
                raw_text TEXT NOT NULL,
                fetched_at REAL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (video_id, language)
            )
        """)
        _CACHE_CONN.commit()
    return _CACHE_CONN


def _cache_key(video_id: str, language: str) -> tuple[str, str]:
    return video_id, language.lower().strip()


def cache_get(video_id: str, language: str) -> str | None:
    """Return cached transcript text or None."""
    try:
        row = _db().execute(
            "SELECT raw_text FROM yt_transcripts WHERE video_id=? AND language=?",
            _cache_key(video_id, language),
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.warning("cache read failed for %s/%s: %s", video_id, language, e)
        return None


def cache_put(video_id: str, language: str, raw_text: str) -> bool:
    """Store transcript in cache. Returns True on success."""
    try:
        _db().execute(
            "INSERT OR REPLACE INTO yt_transcripts (video_id, language, raw_text) VALUES (?, ?, ?)",
            (_cache_key(video_id, language)[0], _cache_key(video_id, language)[1], raw_text),
        )
        _db().commit()
        return True
    except Exception as e:
        log.warning("cache write failed for %s/%s: %s", video_id, language, e)
        return False


# ── Fetching & pagination ───────────────────────────────────────────────

def _fetch_raw(video_id: str, languages: list[str]) -> str | None:
    """Fetch transcript from YouTube API. Returns compacted text or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        yt_api = YouTubeTranscriptApi()
        transcript_list = yt_api.fetch(video_id, languages=languages)
        full_text = " ".join(s.text for s in transcript_list.snippets)
        return _compact(full_text) if full_text else None
    except Exception as e:
        log.warning("transcript fetch failed for %s: %s", video_id, e)
        return None


def _compact(text: str) -> str:
    """Collapse whitespace/newlines so JSON-escaping doesn't eat tokens."""
    if not text:
        return text
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def _chunk_by_words(text: str, chunk_size: int = 5000) -> list[str]:
    """Split text into word-based chunks."""
    words = text.split()
    if not words:
        return []
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunks.append(" ".join(words[i : i + chunk_size]))
    return chunks


def get_transcript(video_id: str, language: str = "en") -> dict:
    """Fetch transcript with cache lookup.

    Returns dict with status and either the full text or an error.
    """
    langs = [l.strip() for l in language.split(",") if l.strip()]
    if not langs:
        langs = ["en"]

    # Try cache first
    cached = cache_get(video_id, language)
    if cached is not None:
        log.info("cache hit for %s/%s", video_id, language)
        return {
            "status": "ok",
            "video_id": video_id,
            "transcript": cached,
            "cached": True,
            "word_count": len(cached.split()) if cached else 0,
            "language": langs[0],
        }

    # Fetch from API
    raw = _fetch_raw(video_id, langs)
    if raw is None:
        return {
            "status": "error",
            "video_id": video_id,
            "error": f"transcript fetch failed for {langs}",
        }

    cache_put(video_id, language, raw)
    log.info("fetched & cached %s/%s (%d words)", video_id, language, len(raw.split()))
    return {
        "status": "ok",
        "video_id": video_id,
        "transcript": raw,
        "cached": False,
        "word_count": len(raw.split()) if raw else 0,
        "language": langs[0],
    }


def paginate_transcript(
    video_id: str, language: str = "en", page_size: int = 5000
) -> dict:
    """Fetch transcript and split into word-based pages.

    Returns paginated result the model can consume page-by-page.
    """
    result = get_transcript(video_id, language)
    if result["status"] != "ok":
        return result

    raw = result["transcript"]
    chunks = _chunk_by_words(raw, chunk_size=page_size)
    total_pages = len(chunks)

    # Return first page + metadata so the model knows there are more
    return {
        "status": "ok",
        "video_id": video_id,
        "language": result["language"],
        "cached": result.get("cached", False),
        "total_pages": total_pages,
        "page_size": page_size,
        "pages": chunks,  # all pages — caller decides how many to send
    }


def get_page(
    video_id: str, language: str = "en", page_num: int = 1, page_size: int = 5000
) -> dict:
    """Fetch a single transcript page by number (1-indexed).

    Uses the full cached/fetched text internally; only returns one page.
    """
    result = get_transcript(video_id, language)
    if result["status"] != "ok":
        return result

    raw = result["transcript"]
    chunks = _chunk_by_words(raw, chunk_size=page_size)
    total_pages = len(chunks)

    if page_num < 1 or page_num > total_pages:
        return {
            "status": "error",
            "video_id": video_id,
            "error": f"Page {page_num} out of range (1-{total_pages})",
        }

    return {
        "status": "ok",
        "video_id": video_id,
        "language": result["language"],
        "cached": result.get("cached", False),
        "total_pages": total_pages,
        "page_num": page_num,
        "page_size": page_size,
        "transcript": chunks[page_num - 1],
    }
