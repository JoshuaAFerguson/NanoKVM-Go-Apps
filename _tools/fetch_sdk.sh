#!/usr/bin/env bash
# Download the upstream appbase SDK into _sdk/ (git-ignored) for local type
# hints and IDE completion. The device ships its own copy in /kvmcomm/apps, so
# never include appbase.py inside an App directory.
set -euo pipefail

ref="${1:-base}"
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/_sdk"
base_url="https://raw.githubusercontent.com/sipeed/NanoKVM-Go-Apps/${ref}"

mkdir -p "$dest"
for file in appbase.py appbase.pyi README.md; do
  curl -fsSL "$base_url/$file" -o "$dest/$file"
done
echo "SDK (${ref}) downloaded to $dest"
