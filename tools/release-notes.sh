#!/usr/bin/env bash
# Prints what a changelog says under a version, to be the notes of that version's release. Fails
# when it says nothing: the changelogs are written by hand, so a release without an entry is one
# somebody forgot to write.
#
#   tools/release-notes.sh panel/CHANGELOG.md 0.1.0
set -euo pipefail
changelog=$1
version=$2

notes=$(awk -v heading="## $version" '/^## / { found = ($0 == heading); next } found' "$changelog")
[[ $notes == *[![:space:]]* ]] || { echo "$changelog has nothing under \"## $version\"" >&2; exit 1; }
printf '%s\n' "$notes"
