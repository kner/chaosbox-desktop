#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$script_dir/run-desktop.sh" --check
exec /usr/bin/python3 "$script_dir/install.py" "$@"
