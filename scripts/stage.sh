#!/usr/bin/env bash
# For a maintainer, while the submission form is down: puts the file of a hand-made pull request
# where its checks look for it, the `staging` prerelease as <sha256>.<extension>.
# Usage: scripts/stage.sh <pull request number> https://github.com/<owner>/<repo>/releases/download/<tag>/<file>
# The file is downloaded, never opened or run, and staged only when its SHA-256 is one that the
# pull request adds to a mods/<id>.toml; the extension is that version's `file`'s.
# Needs `gh` signed in with write access to the registry (GH_REPO, else horizon-rust/horizon-mods).
set -euo pipefail
repo="${GH_REPO:-horizon-rust/horizon-mods}"
pr="${1:-}" url="${2:-}"
if [[ ! "$pr" =~ ^[0-9]+$ ]] \
  || [[ ! "$url" =~ ^https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/releases/download/[^/?#[:space:]]+/[^/?#[:space:]]+$ ]]; then
  echo "usage: $0 <pull request number> https://github.com/<owner>/<repo>/releases/download/<tag>/<file>" >&2
  exit 2
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# `<sha256> <file>` of each version the pull request adds to an entry.
gh pr diff "$pr" --repo "$repo" | awk '
  /^diff --git / { entry = ($3 ~ /^a\/mods\/[a-z0-9_]+\.toml$/); file = ""; sha = ""; next }
  !entry { next }
  /^\+\[\[versions\]\]/ { file = ""; sha = ""; next }
  /^\+file = "[^"]+"$/ { split($0, q, "\""); file = q[2] }
  /^\+sha256 = "[0-9a-f]+"$/ { split($0, q, "\""); if (length(q[2]) == 64) sha = q[2] }
  file != "" && sha != "" { print sha, file; file = ""; sha = "" }
' > "$tmp/added"
if [ ! -s "$tmp/added" ]; then
  echo "#$pr adds no version with a file and a sha256 to mods/" >&2
  exit 1
fi

curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --max-filesize 67108864 --output "$tmp/download" "$url"
if [ "$(wc -c < "$tmp/download")" -gt 67108864 ]; then
  echo "over 64 MiB, the registry's limit" >&2
  exit 1
fi
if command -v sha256sum >/dev/null; then
  sha="$(sha256sum "$tmp/download" | cut -d' ' -f1)"
else
  sha="$(shasum -a 256 "$tmp/download" | cut -d' ' -f1)"
fi
file="$(awk -v sha="$sha" '$1 == sha { print $2; exit }' "$tmp/added")"
if [ -z "$file" ]; then
  echo "SHA-256 $sha is not one #$pr adds:" >&2
  cat "$tmp/added" >&2
  exit 1
fi
ext="${file##*.}"
if [[ ! "$ext" =~ ^[a-z]+$ ]]; then
  echo "$file: no plain extension" >&2
  exit 1
fi
name="$sha.$ext"
if gh release view staging --repo "$repo" --json assets --jq '.assets[].name' | grep -Fxq "$name"; then
  echo "staging already has $name"
  exit 0
fi
mv "$tmp/download" "$tmp/$name"
gh release upload staging "$tmp/$name" --repo "$repo"
echo "Staged $name for #$pr ($file): re-run its check."
