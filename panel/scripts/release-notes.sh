#!/usr/bin/env bash
# Prints what CHANGELOG.md says under a version, to be the notes of that version's release. Fails
# when it says nothing: the changelog is written by hand, so a release without an entry is one
# somebody forgot to write.
set -euo pipefail
cd "$(dirname "$0")/.."

notes=$(awk -v heading="## $1" '/^## / { found = ($0 == heading); next } found' CHANGELOG.md)
[[ $notes == *[![:space:]]* ]] || { echo "CHANGELOG.md has nothing under \"## $1\"" >&2; exit 1; }
printf '%s\n' "$notes"
