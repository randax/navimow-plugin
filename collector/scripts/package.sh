#!/usr/bin/env bash
# Builds what a release publishes to the package index, the wheel and the source distribution
# in dist/, and installs the wheel into an environment of its own to see that it brings the
# navimow-collector command with it.
#
# Given a version, as the release workflow gives the one in the tag, it fails unless that is
# the version built, so a tag can never publish a package that calls itself something else.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf dist
python -m build --outdir dist >/dev/null
python -m twine check --strict dist/*

wheels=(dist/navimow_collector-*.whl)
wheel=${wheels[0]}
version=${wheel#dist/navimow_collector-}
version=${version%%-*}
if [[ -n ${1:-} && $1 != "$version" ]]; then
  echo "asked to package $1, but pyproject.toml says $version: its version must match the tag" >&2
  exit 1
fi

environment=$(mktemp -d)
trap 'rm -rf "$environment"' EXIT
python -m venv "$environment"
"$environment/bin/pip" install --quiet "$wheel"
"$environment/bin/navimow-collector" config >/dev/null
echo "$wheel installs and runs"
