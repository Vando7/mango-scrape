"""Workspace file operation tests against a running scraper server."""

import pytest


async def test_download_file(http_client):
    """Download a small text file from a public URL."""
    r = await http_client.post(
        "/download", json={"url": "https://www.w3.org/Addressing/URL/url-spec.txt"}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    assert data["status"] == "ok", f"Error: {data.get('error')}"
    assert "path" in data
    assert "filename" in data
    assert isinstance(data["size"], int) and data["size"] > 0


async def test_download_invalid_url(http_client):
    """Download from an invalid URL should return error."""
    r = await http_client.post(
        "/download", json={"url": "https://thisdomaindoesnotexist12345.com/file.txt"}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    # Should gracefully return error status
    if "status" in data:
        assert data["status"] != "ok" or "error" in data


async def test_clone_repo(http_client):
    """Clone endpoint returns proper structure (auth may not be configured)."""
    r = await http_client.post(
        "/clone",
        json={"git_url": "https://github.com/python/cpython.git"},
        timeout=120,
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    # Clone may fail if git auth isn't configured — just check structure
    assert "status" in data
    assert "git_url" not in data or data.get("git_url") == "https://github.com/python/cpython.git"
    if data["status"] == "ok":
        assert "path" in data
        assert isinstance(data["file_count"], int)


async def test_clone_invalid_repo(http_client):
    """Clone an invalid repo should return error."""
    r = await http_client.post(
        "/clone", json={"git_url": "https://github.com/thisuserdoesnotexist12345/nonexistent-repo.git"}
    )
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    if "status" in data:
        assert data["status"] != "ok" or "error" in data


async def test_list_files_root(http_client):
    """List files in the workspace root."""
    r = await http_client.post("/list", json={"path": "."})
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    assert data["status"] == "ok", f"Error: {data.get('error')}"
    assert isinstance(data["total_entries"], int)


async def test_list_files_nonexistent(http_client):
    """List a nonexistent path should return error."""
    r = await http_client.post("/list", json={"path": "/nonexistent/path"})
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    if "status" in data:
        assert data["status"] != "ok" or "error" in data


async def test_cat_file(http_client):
    """Read a file from the workspace (download first, then cat)."""
    # Download a small text file first
    r_dl = await http_client.post(
        "/download",
        json={"url": "https://www.w3.org/Addressing/URL/url-spec.txt"},
    )
    assert r_dl.status_code == 200, f"Download failed: {r_dl.text[:500]}"
    dl_data = r_dl.json()
    if dl_data.get("status") != "ok":
        pytest.skip(f"Download failed: {dl_data.get('error')}")

    # Now cat the downloaded file
    fname = dl_data["filename"]
    r_cat = await http_client.post("/cat", json={"path": fname})
    assert r_cat.status_code == 200, f"Cat failed: {r_cat.text[:500]}"
    data = r_cat.json()
    assert data["status"] == "ok", f"Error: {data.get('error')}"
    assert "content" in data
    assert isinstance(data["size"], int) and data["size"] > 0


async def test_cat_nonexistent_file(http_client):
    """Cat a nonexistent file should return error."""
    r = await http_client.post("/cat", json={"path": "/nonexistent/file.txt"})
    assert r.status_code == 200, f"Status: {r.status_code}, body: {r.text[:500]}"
    data = r.json()
    if "status" in data:
        assert data["status"] != "ok" or "error" in data
