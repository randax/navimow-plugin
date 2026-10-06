# Navimow map panel

A Grafana panel that draws a Navimow mower's Trail on a real map of the lawn. Frontend only,
built on MapLibre GL. Apache-2.0. Spec: [#25](https://github.com/randax/navimow-plugin/issues/25).

## Install

The panel is not in Grafana's plugin catalogue and is not signed, so it is installed by hand, into
a Grafana you run yourself, version 12.4 or later. Grafana Cloud refuses unsigned plugins and
cannot load it.

1. Download `randax-navimowmap-panel-<version>.zip` from the newest release called **Panel** on the
   [releases page](https://github.com/randax/navimow-plugin/releases). The `.sha256` beside it
   checks the download: `shasum -a 256 -c randax-navimowmap-panel-<version>.zip.sha256`.
2. Unpack it into Grafana's plugin directory, `/var/lib/grafana/plugins` unless
   [`paths.plugins`](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#plugins)
   says otherwise, so that it holds `randax-navimowmap-panel/plugin.json`.
3. Let Grafana load it unsigned. This is the one setting the panel needs, in `grafana.ini`:

   ```ini
   [plugins]
   allow_loading_unsigned_plugins = randax-navimowmap-panel
   ```

   For a container, the same as an environment variable:
   `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=randax-navimowmap-panel`. The value is a
   comma-separated list of plugin identifiers, so add to any already there.

4. Restart Grafana. **Navimow Map** is then among the visualisations.

Without the setting Grafana skips the plugin and says so in its log:
`Skipping loading plugin due to problem with signature pluginId=randax-navimowmap-panel status=unsigned`.

If Grafana's content security policy is enabled, the map stays blank until the tile hosts are
permitted: see [Tile hosts](#tile-hosts).

The same release carries the bundled dashboards, to import once the panel is in: see
[the dashboards](../dashboards/README.md). What the panel reads and what its options do is in
[`src/README.md`](src/README.md), which Grafana shows on the plugin's own page.

**Why unsigned.** Grafana signs a plugin for everyone only through its catalogue. A signature made
outside it is bound to the URLs of one Grafana, so an archive signed here would be refused by every
other installation.

## Develop

```bash
pnpm install
pnpm run dev                 # build and watch into dist/
docker compose up -d --build # Grafana on http://localhost:3000 with dist/ mounted
```

`docker compose` also starts a PostgreSQL and fills it as the collector would, by replaying the
real capture twice (`tests/seed.py`): a Job that ended some hours ago, and the same Job a week
before with an error and a gap in collection put into it. That is what the bundled dashboard in
[`../dashboards`](../dashboards/README.md) reads, at `/d/navimow`. The first start installs the
collector into the seeding container, which takes a minute. The Jobs are placed relative to when
the database was filled, so on a later day fill it again, to have one in the last 24 hours and for
the browser tests of that dashboard to pass: `docker compose run --rm seed`.

Provisioned dashboards (`provisioning/`, development only):

- `/d/navimow-map`: every Base map kind side by side, including a custom WMS template and a
  custom source without attribution.
- `/d/navimow-map-lifecycle`: one map inside a collapsible row, used to check maps are released.
- `/d/navimow-trail`: a slice of the real Trail in `fixtures/trail-2026-09-21.csv` (from 13:30 UTC,
  in delivery order) fed through TestData, and the same data without matching column names.
- `/d/navimow-terrain`: the same Trail with Terrain, starting flat and starting in terrain.
- `/d/navimow-overlay`: the same Trail with the hillshade Overlay at full opacity.
- `/d/navimow-coverage`: the same Trail with Coverage as a Grid, a Heatmap and a Buffered line, and
  raised over Terrain.

`plugin.json` changes need a Grafana restart: `docker compose restart`.

## Test

```bash
pnpm run typecheck && pnpm run lint
pnpm run test:ci                       # model functions (Jest, no browser)
pnpm exec playwright install chromium  # once
pnpm run e2e                           # against the running Grafana, fixture tiles
LIVE_TILES=1 pnpm run e2e              # the same against the real tile services (nightly in CI)
```

Two seams only. Geometry, source resolution and the map's style live in `src/model/` as pure
functions that never load MapLibre (ESLint enforces it), and are tested with Jest. Everything visual
is tested in a real browser against the provisioned dashboards. Headless Chromium draws WebGL
through SwiftShader (see `playwright.config.ts`), so the first frame is slow.

The fixture tiles in `tests/fixtures/` are plain colours a test can count: a green Base map, a blue
Overlay, and Terrain that is 400 m high everywhere. Tests that count pixels are skipped with
`LIVE_TILES=1`.

## Terrain

Terrain is declared in a map's first style, never added to a map already drawn: the terrain
prototype found that MapLibre then keeps the camera's height above sea level, throwing the view
outward by the height of the ground. So switching between flat and terrain, or changing the
Terrain's source, recreates the map, and the new one starts from the old one's camera (`cameraFor`
in `src/model/view.ts`).

## Build arrangement

`.config/` is generated by `@grafana/create-plugin` and never edited. `webpack.config.ts` merges
over it and copies MapLibre's worker and shared modules into `dist/`, so the worker loads from
the plugin's own origin. The copy uses absolute source paths: relative ones resolve against
`src/` and fail.

## Tile hosts

Tiles load directly from the browser, so every tile host must allow cross-origin requests.
Grafana's content security policy is off by default. If it is enabled, add the hosts of the Base
map, Terrain and Overlay in use to the `connect-src` of `content_security_policy_template`, under
`[security]` in `grafana.ini`:

| Base map                          | Host                             |
| --------------------------------- | -------------------------------- |
| Kartverket topo, gråtone, turkart | `https://cache.kartverket.no`    |
| OpenStreetMap                     | `https://tile.openstreetmap.org` |
| Custom                            | the host in its URL template     |

| Terrain           | Host                           |
| ----------------- | ------------------------------ |
| Mapterhorn        | `https://tiles.mapterhorn.com` |
| AWS Terrain Tiles | `https://s3.amazonaws.com`     |
| Custom            | the host in its URL template   |

| Overlay              | Host                         |
| -------------------- | ---------------------------- |
| Kartverket hillshade | `https://wms.geonorge.no`    |
| Custom               | the host in its URL template |

A panel with the default Base map, Terrain enabled and the hillshade Overlay therefore needs
`connect-src` extended with `https://cache.kartverket.no https://tiles.mapterhorn.com https://wms.geonorge.no`.
Terrain's host is only contacted while a map is in its terrain view.

Nothing else needs a policy change: the MapLibre worker is served from the plugin's own origin.

## Release

The panel has its own version, the one in `package.json`, and its own tags, `panel/v<version>`. The
collector's are `collector/v<version>`, and releasing either never releases the other:
[why that holds](../README.md#two-versions).

1. Set `version` in `package.json`, and in `CHANGELOG.md` rename `## Unreleased` to
   `## <version>`. The changelog is written by hand, as changes are made. Give the same version to
   the `pluginVersion` of the panel saved in an earlier shape in
   `provisioning/dashboards/navimow-trail.json`: Grafana migrates a panel saved under any other
   version, which that panel is there to avoid, and its browser test fails until the two agree.
   Merge.
2. Tag the merged commit and push the tag: `git tag panel/v<version> && git push origin panel/v<version>`.

`.github/workflows/panel-release.yml` then builds the panel, packs it (`scripts/package.sh`), loads
the archive into a stock Grafana with nothing set but the setting above (`scripts/smoke.sh`), and
publishes a GitHub release holding the archive, its SHA-256 and the bundled dashboards, with that
version's changelog entries as its notes. It publishes nothing if the tag and `package.json`
disagree, or if the changelog has no entries for the version. Every pull request packs and loads
the archive too, so a tag is never the first time that runs. To run the two scripts by hand, after
`pnpm run build`, they need `jq`, `zip` and Docker.

## Licence

Apache-2.0, in [`LICENSE`](LICENSE) and in the archive. The collector in the same repository is
GPL-3.0-only, and [the repository's README](../README.md#two-licences) explains the split.
