"""Unit tests for transcript cache, pagination, and compact — no Docker needed."""

import os
import tempfile
from pathlib import Path

from yt_transcript import _compact, _chunk_by_words, get_page, paginate_transcript


# ── Fixtures ─────────────────────────────────────────────────────────────

def _temp_db():
    """Create a temp SQLite DB and return the path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_compact_strips_extra_whitespace():
    assert _compact("hello   world\n\n\n  foo  bar  ") == "hello world\nfoo bar"


def test_compact_empty_input():
    assert _compact("") == ""
    assert _compact(None) is None


def test_chunk_by_words_basic():
    words = ["a"] * 1050
    chunks = _chunk_by_words(" ".join(words), chunk_size=500)
    assert len(chunks) == 3
    assert len(chunks[0].split()) == 500
    assert len(chunks[1].split()) == 500
    assert len(chunks[2].split()) == 50


def test_chunk_by_words_exact_division():
    words = ["a"] * 1000
    chunks = _chunk_by_words(" ".join(words), chunk_size=500)
    assert len(chunks) == 2
    assert len(chunks[0].split()) == 500


def test_chunk_by_words_empty():
    assert _chunk_by_words("") == []


def test_paginate_transcript_structure():
    """paginate_transcript returns correct structure (no API call needed — uses cache)."""
    # First, populate the cache with a known value
    from yt_transcript import cache_put
    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        test_text = " ".join(["hello world this is page content"] * 10)
        cache_put("test_vid", "en", test_text)

        result = paginate_transcript("test_vid", "en")
        assert result["status"] == "ok"
        assert result["cached"] is True
        assert result["total_pages"] > 1
        assert len(result["pages"]) == result["total_pages"]
    finally:
        os.unlink(db_path)


def test_get_page_single():
    """get_page returns one page with correct metadata."""
    from yt_transcript import cache_put, get_page

    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        # Single-page transcript (under 500 words)
        short_text = " ".join(["word"] * 100)
        cache_put("short_vid", "en", short_text)

        result = get_page("short_vid", "en", page_num=1, page_size=500)
        assert result["status"] == "ok"
        assert result["page_num"] == 1
        assert result["total_pages"] == 1
    finally:
        os.unlink(db_path)


def test_get_page_out_of_range():
    from yt_transcript import cache_put, get_page

    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        cache_put("vid", "en", " ".join(["word"] * 100))
        result = get_page("vid", "en", page_num=5, page_size=500)
        assert result["status"] == "error"
        assert "out of range" in result["error"].lower()
    finally:
        os.unlink(db_path)


def test_get_page_zero():
    from yt_transcript import cache_put, get_page

    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        cache_put("vid", "en", " ".join(["word"] * 100))
        result = get_page("vid", "en", page_num=0, page_size=500)
        assert result["status"] == "error"
    finally:
        os.unlink(db_path)


def test_cache_persists_across_get_put():
    """Put then get should return the same text."""
    from yt_transcript import cache_get, cache_put

    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        text = "hello world this is a test transcript for caching"
        cache_put("vid", "en", text)
        assert cache_get("vid", "en") == text
    finally:
        os.unlink(db_path)


def test_cache_different_languages():
    """Different languages should be separate entries."""
    from yt_transcript import cache_get, cache_put

    db_path = _temp_db()
    os.environ["DEEP_DIVE_CACHE_DB"] = db_path

    try:
        cache_put("vid", "en", "english text")
        cache_put("vid", "bg", "bulgarian text")
        assert cache_get("vid", "en") == "english text"
        assert cache_get("vid", "bg") == "bulgarian text"
    finally:
        os.unlink(db_path)
