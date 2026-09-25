#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo 'Python 3.11 or newer is required.' >&2
  exit 1
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '\nFreshhead: http://127.0.0.1:8766\nKeep this terminal open. Ctrl+C to stop.\n\n'
exec .venv/bin/python -m freshhead
