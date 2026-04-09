#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${XARTICLE_READER_PYTHON:-python3}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/x_article_reader.py" "$@"

