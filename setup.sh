#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo ">>> uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

UV="$(command -v uv)"
echo ">>> using uv at $UV"

echo ">>> installing scraper deps ($REPO_DIR)"
"$UV" sync

echo ">>> installing patched Chromium"
"$UV" run patchright install chromium

echo ">>> installing shim deps ($REPO_DIR/shim)"
"$UV" sync --project "$REPO_DIR/shim"

MCP_JSON="$REPO_DIR/mcp.json"
cat > "$MCP_JSON" <<EOF
{
  "mcpServers": {
    "deep-dive": {
      "command": "$UV",
      "args": [
        "run",
        "--project",
        "$REPO_DIR/shim",
        "python",
        "$REPO_DIR/shim/mcp_shim.py"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "DEEP_DIVE_URL": "http://localhost:8765"
      }
    }
  }
}
EOF
echo ">>> wrote $MCP_JSON"

echo
echo "Done. To wire into LM Studio, merge the deep-dive entry from"
echo "  $MCP_JSON"
echo "into ~/.lmstudio/mcp.json (Windows: %USERPROFILE%\\.lmstudio\\mcp.json)."
echo
echo "Sanity check the scraper directly:"
echo "  uv run uvicorn server:app --port 8765"
echo "  curl -s http://localhost:8765/health"
