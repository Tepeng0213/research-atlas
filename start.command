#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -B -m workbench
fi
exec /usr/bin/python3 -B -m workbench
