#!/usr/bin/env bash
# Run tests against a running scraper server.
# Usage: ./tests/run_tests.sh [filter]
#   e.g.: ./tests/run_tests.sh test_scrape  # run only scrape tests

set -e

cd "$(dirname "$0")/.."

export SCRAPER_SERVER_URL="${SCRAPER_SERVER_URL:-http://localhost:8765}"

echo "Running tests against ${SCRAPER_SERVER_URL} ..."
python -m pytest "$@" tests/
