#!/usr/bin/env bash
# Cross-platform setup (Linux/macOS). Windows: setup.bat or python setup_env.py
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  exec python3 setup_env.py "$@"
fi
exec python setup_env.py "$@"
