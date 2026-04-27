# API Tests — run against the live Docker container

## Quick start

```bash
# Make sure the scraper is running:
cd .. && docker compose up -d

# Run all tests:
./tests/run_tests.sh

# Run a subset:
./tests/run_tests.sh test_scrape.py
./tests/run_tests.sh test_scrape::test_scrape_single_article
```

## Test groups

| File | What it tests |
|---|---|
| `test_health.py` | `/health` endpoint |
| `test_scrape.py` | General URL scraping (Wikipedia, Reuters, Python blog, Reddit) |
| `test_screenshot.py` | Full-page screenshots |
| `test_youtube.py` | YouTube transcript + comments extraction |
| `test_workspace.py` | File ops: download, git clone, list files, cat file |
| `test_errors.py` | Error handling: invalid URLs, empty input, mixed valid/invalid |

## Environment variables

- `SCRAPER_SERVER_URL` — API base URL (default: `http://localhost:9161`)

## Dependencies

Install test deps in the venv:

```bash
cd .. && pip install pytest pytest-asyncio httpx
```
