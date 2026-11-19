#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [[ -x .venv-linux/bin/python ]]; then
  exec .venv-linux/bin/python run.py
fi
if [[ -x "$(dirname "$0")/.venv-linux/bin/python" ]]; then
  exec "$(dirname "$0")/.venv-linux/bin/python" "$(dirname "$0")/run.py"
fi
echo "Environment missing. Run ./setup-linux.sh first."
exit 1
