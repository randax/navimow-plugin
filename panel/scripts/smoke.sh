#!/usr/bin/env bash
# Installs the packaged archive the way the README tells an owner to, into a stock Grafana with
# nothing set but allow_loading_unsigned_plugins, and fails unless Grafana loads the plugin from it.
set -euo pipefail
cd "$(dirname "$0")/.."

id=randax-navimowmap-panel
archives=(artifacts/"$id"-*.zip)
archive=${archives[0]}
[[ -f $archive ]] || { echo "no archive in artifacts/: run scripts/package.sh first" >&2; exit 1; }
(cd artifacts && shasum -a 256 -c "$(basename "$archive").sha256")
version=${archive#"artifacts/$id-"}
version=${version%.zip}

rm -rf work/plugins
mkdir -p work/plugins
unzip -q "$archive" -d work/plugins

container=$(docker run -d -p 127.0.0.1::3000 \
  -v "$PWD/work/plugins:/var/lib/grafana/plugins:ro" \
  -e GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS="$id" \
  "grafana/${GRAFANA_IMAGE:-grafana-enterprise}:${GRAFANA_VERSION:-13.1.0}")
trap 'docker rm -f "$container" >/dev/null' EXIT
url=http://$(docker port "$container" 3000/tcp)

for _ in $(seq 60); do
  curl -sf "$url/api/health" >/dev/null && break
  sleep 1
done

loaded=$(curl -sf -u admin:admin "$url/api/plugins/$id/settings") || {
  echo "Grafana did not load $id from $archive" >&2
  docker logs "$container" 2>&1 | grep -i "$id" >&2 || true
  exit 1
}
jq -e --arg version "$version" '.info.version == $version and .signature == "unsigned"' <<<"$loaded" >/dev/null || {
  echo "Grafana loaded $id, but not as an unsigned $version:" >&2
  jq '{version: .info.version, signature}' <<<"$loaded" >&2
  exit 1
}
echo "Grafana loaded $id $version, unsigned, from $archive"
