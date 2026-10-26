#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip install --no-deps -e .
fi
.venv/bin/python -m farstar setup
exec .venv/bin/python -m farstar "${1:-run}"
