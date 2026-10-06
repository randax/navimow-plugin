#!/usr/bin/env bash
# Packs a built dist/ as what a release publishes: artifacts/<plugin id>-<version>.zip, holding one
# directory named after the plugin id as Grafana expects, and the archive's SHA-256 beside it.
#
# Given a version, as the release workflow gives the one in the tag, it fails unless dist/ was
# built at that version, so a tag can never publish an archive that calls itself something else.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f dist/plugin.json ]] || { echo "no dist/: run pnpm run build first" >&2; exit 1; }
id=$(jq -r .id dist/plugin.json)
version=$(jq -r .info.version dist/plugin.json)

if [[ $(jq -r .buildMode dist/plugin.json) != production ]]; then
  echo "dist/ is not a production build: run pnpm run build" >&2
  exit 1
fi
if [[ -n ${1:-} && $1 != "$version" ]]; then
  echo "asked to package $1, but dist/ was built at $version: the version in package.json must match the tag" >&2
  exit 1
fi

rm -rf artifacts "work/$id"
mkdir -p artifacts "work/$id"
cp -R dist/. "work/$id"

archive=$id-$version.zip
(cd work && zip -qr "../artifacts/$archive" "$id")
(cd artifacts && shasum -a 256 "$archive" >"$archive.sha256")
echo "artifacts/$archive"
