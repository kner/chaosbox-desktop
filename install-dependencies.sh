#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -r /etc/os-release ]]; then
    echo "Cannot identify the operating system (/etc/os-release is missing)." >&2
    exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
    echo "This script supports Ubuntu 24.04 only (detected: ${PRETTY_NAME:-unknown})." >&2
    exit 1
fi

if (( EUID == 0 )); then
    apt=(apt-get)
elif command -v sudo >/dev/null 2>&1; then
    apt=(sudo apt-get)
else
    echo "Run this script as root or install sudo and run it again." >&2
    exit 1
fi

packages=(
    python3
    python3-tk
    python3-pil
    python3-pil.imagetk
    python3-paramiko
    python3-gi
    gir1.2-gexiv2-0.10
    python3-mutagen
    python3-markdown-it
    ffmpeg
    fonts-dejavu-core
    openssh-client
)

"${apt[@]}" update
"${apt[@]}" install -y "${packages[@]}"

"$script_dir/run-desktop.sh" --check
