# Playwright's Python image already includes all the system shared libs Chromium needs
# (libatk, libnss, libcups, etc.) — saves the apt dance we hit during stdio setup.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir \
    patchright \
    trafilatura \
    fastapi \
    "uvicorn[standard]" \
    pydantic

# Download patchright's patched Chromium (separate binary from the stock playwright one).
RUN patchright install chromium

COPY scraper.py server.py ./

EXPOSE 8765

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
