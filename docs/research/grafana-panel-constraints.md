# Grafana panel plugin constraints for a MapLibre GL panel

Resolves [#3](https://github.com/randax/navimow-plugin/issues/3) (part of the wayfinder map [#1](https://github.com/randax/navimow-plugin/issues/1)).
Researched 2026-09-15 against primary sources: grafana.com/developers docs, the `@grafana/create-plugin@7.10.1` tarball, `grafana/grafana` at `v13.2.1` and `main`, `grafana/scenes` `main`, `maplibre/maplibre-gl-js` at `v6.9.1`, the MapLibre style spec, Kartverket/Geonorge pages, and live HTTP checks. Each claim carries its source; "verified live" means a `curl` run on the research date.

## 1. Constraints list (the short version)

| # | Constraint | Consequence for us |
|---|---|---|
| C1 | Panel plugins have **no backend, no `routes`, no proxy endpoint**. Only datasource (`/api/datasources/proxy/uid/:uid/*`) and app (`/api/plugin-proxy/:pluginId/*`) plugins get one. | Tiles are fetched **browser-direct**. Tile servers must be HTTPS and CORS-enabled. No API-key hiding possible. |
| C2 | MapLibre loads raster tiles via `fetch()` + `createImageBitmap`, never plain `<img>` in default config. | Every tile host needs `Access-Control-Allow-Origin`. Kartverket (`cache.kartverket.no`) and Mapterhorn send `*` (verified live). |
| C3 | Grafana's CSP is **off by default** (`[security] content_security_policy = false`). When on, `connect-src` is `'self' grafana.com [*.cartocdn.com] ws:// wss://` and there is no `worker-src`. | Works on stock self-hosted Grafana. Under CSP the operator must add tile hosts to `connect-src`; the worker must be same-origin (not `blob:`). Document in README. |
| C4 | MapLibre **v6 is ESM-only, requires WebGL2**, and loads its worker from a real URL (`maplibre-gl-worker.mjs` + `maplibre-gl-shared.mjs`). create-plugin has **no web-worker support** (plugin-tools#2007 open). | Copy the two `.mjs` files into `dist/` via the webpack extension hook and call `setWorkerUrl(__webpack_public_path__ + 'maplibre-gl-worker.mjs')`. Wrap `new Map()` in `try/catch (GPUInitializationError)` with a text fallback. |
| C5 | Browsers cap live WebGL contexts (~8–16); the oldest is silently dropped. | `map.remove()` in the React cleanup, one map per panel, guard against many map panels on one dashboard. |
| C6 | React is **shared with Grafana** (externals). Grafana ≥13.2 runs React 19; create-plugin 7.10.1 still pins React 18 types and `@grafana/*@13.1.0`, default `grafanaDependency ">=12.3.0"`. | Target `grafanaDependency ">=12.4.0"` (all supported lines) with dual-compatible React code, or `">=13.2.0"` to use React 19 freely. Never bundle React. |
| C7 | Default webpack config: single `module.js`, minified in production, no `splitChunks`, no `performance` budget; async `import()` chunks **do** work (runtime `__webpack_public_path__` from AMD `module.uri`). | MapLibre adds ~295 KB gzip; acceptable in `module.js`, or lazy-load via `import(/* webpackChunkName: "maplibre" */ ...)`. |
| C8 | Options editors are plain React components inside a ~330 px (user-resizable) right pane, always wrapped in a `<Field>`. `Modal`/`Drawer` from `@grafana/ui` are the sanctioned escape. | A polygon/calibration editor lives either in a `Drawer size="lg"` or, better, **on the panel map itself**, with `PanelContext.onInstanceStateChange` + `context.instanceState` bridging panel and editor (Geomap's pattern). |
| C9 | `PanelProps` gives `data.series: DataFrame[]`, `timeRange`, `replaceVariables`, `onOptionsChange`, `onChangeTimeRange`, `width/height`, `eventBus`. Variable changes re-render (options) / re-query (queries) automatically; the panel is **not rendered while width is 0**. | Diff sub-objects of `options`, not the whole object, or the map is torn down on every variable tick. |
| C10 | Frame shape depends on datasource format: SQL `table` = one frame per query, one field per column; SQL `time_series` = wide frame; InfluxDB `time_series` = **one frame per (series × value column)**. `prepareTimeSeries` is app-internal; `outerJoinDataFrames` (deprecated alias of `joinDataFrames`) is exported. | Recommend SQL `table` format (`time, x, y/lat, lon, job_id, ...`). Support Influx by joining frames on Time with `outerJoinDataFrames`. |
| C11 | Unsigned plugins load on self-hosted Grafana with `[plugins] allow_loading_unsigned_plugins = <exact plugin id>` (env `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS`). `app_mode = development` bypasses the check entirely. Private signing (`--rootUrls`) still exists. | Ship unsigned for personal use; keep `app_mode = production`. Signing is out of scope (map Notes). |
| C12 | No Norwegian terrain-RGB tile service exists. Mapterhorn serves Kartverket's 1 m DTM as global `terrarium` tiles (z ≤ 16, CORS `*`). Norge i bilder orthophoto is closed (GeoID, IP-bound tokens). | 3D terrain via Mapterhorn `raster-dem`; drop orthophoto from scope unless self-hosted. |
| C13 | Kartverket topo WMTS: `https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png` — **`{y}` before `{x}`**, PNG only, CC BY 4.0, attribution `© Kartverket`. Layers: `topo`, `topograatone`, `toporaster`, `sjokartraster`. | Ship these four as presets. Wrong axis order yields blank tiles silently. |
| C14 | `tile.openstreetmap.org` is CORS-open but the usage policy blocks generic/heavy clients and returns block *images* with HTTP 200 (`x-blocked` header). | OSM as default outside Norway is risky; make the XYZ URL configurable and warn in README. Consider a commercial/self-hosted default. |

## 2. Toolchain: create-plugin, versions, target Grafana

### 2.1 Scaffold

- Current `@grafana/create-plugin` is **7.10.1** (2026-09-10; CLI needs Node ≥ 20, generated project `engines.node >= 22`). — [npm](https://registry.npmjs.org/@grafana/create-plugin/latest)
- Command (pnpm): `pnpm dlx @grafana/create-plugin@latest`. Non-interactive: `pnpm dlx @grafana/create-plugin@latest --pluginType=panel --pluginName="Navimow Map" --orgName=randax`. The package manager is detected from `npm_config_user_agent` and written to `packageManager` in `package.json`. — [plugin-tools docs](https://grafana.com/developers/plugin-tools/), `dist/utils/utils.packageManager.js` in the tarball
- Panel scaffold asks three prompts: plugin type, plugin name, org name (`hasBackend` is skipped for panels). Plugin id becomes `<org>-<name>-panel`, e.g. `randax-navimow-panel`. — `dist/commands/generate/prompt-user.js`
- Generated layout: `src/module.ts`, `src/plugin.json`, `src/types.ts`, `src/components/SimplePanel.tsx`, `tests/panel.spec.ts` (Playwright + `@grafana/plugin-e2e`), `provisioning/`, `docker-compose.yaml`, and an auto-generated `.config/` (webpack, rspack, jest, eslint, tsconfig, Dockerfile) that must **not** be edited. Extension is via root `webpack.config.ts` using `webpack-merge` (not in devDependencies; add it) and repointing the `build`/`dev` scripts. — `.config/README.md`, [extend-configurations](https://grafana.com/developers/plugin-tools/how-to-guides/extend-configurations)
- Scripts: `build`, `dev` (watch + livereload), `test`, `test:ci`, `typecheck`, `lint`, `lint:fix`, `e2e`, `server` (`docker compose up --build`), `sign`.
- `.npmrc` has `ignore-scripts=true`.

### 2.2 Versions and pins

- Latest stable Grafana is **13.2.1** (2026-09-02). Supported lines: 12.4.x (until 2027-05), 13.0.x, 13.1.x, 13.2.x. — [GitHub releases](https://api.github.com/repos/grafana/grafana/releases), [when-to-upgrade](https://grafana.com/docs/grafana/latest/upgrade-guide/when-to-upgrade/)
- create-plugin 7.10.1 pins `@grafana/{data,ui,runtime,schema,i18n}@13.1.0`, `react@^18.3.0`, TypeScript 5.9.2, webpack ^5.101, Jest 29, Playwright ^1.62. Latest `@grafana/*` is 13.2.1 with a React `>=19` peer dep. — scaffold `package.json`; [migrate 13.1→13.2](https://grafana.com/developers/plugin-tools/migration-guides/update-from-grafana-versions/migrate-13_1_x-to-13_2_x)
- Default `plugin.json` `dependencies.grafanaDependency` written by the scaffold is `">=12.3.0"`. Required fields: `type`, `name`, `id`, `info{logos,version,updated,keywords}`, `dependencies{grafanaDependency}`. Panel-relevant optional fields: `skipDataQuery`, `suggestions`, `state`. `autoEnabled`, `routes`, `backend` are app/datasource only. — [plugin.json reference](https://grafana.com/developers/plugin-tools/reference/plugin-json), [schema](https://raw.githubusercontent.com/grafana/grafana/v13.2.1/docs/sources/developers/plugins/plugin.schema.json)

**Recommendation:** target `grafanaDependency: ">=12.4.0"` and dev-test against 13.2.x (`GRAFANA_VERSION=13.2.1 pnpm run server`). React is external, so the plugin runs on whatever React the host ships: keep code React 18/19 dual-compatible (no React-19-only APIs). Move to `>=13.2.0` once 12.4 leaves support (May 2027).

### 2.3 Webpack output, externals, code splitting

- Output is AMD (`library.type: 'amd'`) loaded by Grafana's SystemJS; `publicPath` is `public/plugins/<id>/` but overridden at runtime from the AMD `module.uri` via a virtual module imported into `module.ts`. Hence **dynamic `import()` chunks work**, with production `chunkFilename '[name].js?_cache=[contenthash]'` (query-string cache buster, so name chunks with `webpackChunkName`). `crossOriginLoading: 'anonymous'` + SRI. — `.config/webpack/webpack.config.ts`
- Externals (never bundled): `react`, `react-dom`, `react/jsx-runtime`, `@grafana/ui|runtime|data`, `@emotion/*`, `lodash`, `rxjs`, `d3`, `moment`, `i18next`, `react-router`, `redux`, `react-redux`. `maplibre-gl` is **not** external. — `.config/bundler/externals.ts`, [npm-dependencies](https://grafana.com/developers/plugin-tools/key-concepts/npm-dependencies)
- Production: Terser minification, `drop_console: ['log','info']`, `source-map`. No `splitChunks`, no `performance` block (webpack's 250 KiB warning default applies). `swc-loader` targeting ES2015. CSS: `style-loader` + `css-loader` already configured, so `import 'maplibre-gl/dist/maplibre-gl.css'` works. `experiments.asyncWebAssembly: true`. `copy-webpack-plugin` is wired via `.config/bundler/copyFiles.ts`.
- CI includes `bundle-stats.yml` (bundle-size delta comments on PRs). No documented hard bundle-size limit; `plugin-validator` has no size pass.

## 3. How the panel receives data, time range and variables

`PanelProps<T>` ([grafana-data/src/types/panel.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/types/panel.ts)): `id`, `data: PanelData` (`series: DataFrame[]`, `state`, `annotations?`, `timeRange`, `structureRev`), `timeRange`, `timeZone`, `options`, `fieldConfig`, `width`, `height`, `transparent`, `title`, `eventBus`, `onOptionsChange`, `onFieldConfigChange`, `replaceVariables(str, scopedVars?, format?)`, `onChangeTimeRange({from,to})`. Rendered by `VizPanelRenderer` in `grafana/scenes`; **not mounted while `width === 0`**, so the map container always has a size on first mount. — [VizPanelRenderer.tsx](https://github.com/grafana/scenes/blob/main/packages/scenes/src/components/VizPanel/VizPanelRenderer.tsx)

- **Variables**: `VizPanel` declares `VariableDependencyConfig` on `title/options/fieldConfig` → re-render (not re-query) when a referenced variable changes; `SceneQueryRunner` re-queries when a variable used in queries changes. The panel must call `replaceVariables` itself on option strings (e.g. a tile URL containing `$var`). — [VizPanel.tsx](https://github.com/grafana/scenes/blob/main/packages/scenes/src/components/VizPanel/VizPanel.tsx), [SceneQueryRunner.ts](https://github.com/grafana/scenes/blob/main/packages/scenes/src/querying/SceneQueryRunner.ts)
- **Time**: `onChangeTimeRange` sets the **dashboard** time range (absolute). Useful for "zoom to this Job".
- **Cross-panel hover**: `usePanelContext()` gives `sync()`, `eventBus`; publish/subscribe `DataHoverEvent` / `DataHoverClearEvent` with `payload.point.time` to highlight the mower position at the time hovered in a battery graph. Guard on `sync() !== DashboardCursorSync.Off`. — [events/common.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/events/common.ts), [EventBusPlugin.tsx](https://github.com/grafana/grafana/blob/main/packages/grafana-ui/src/components/uPlot/plugins/EventBusPlugin.tsx)
- **Frames**: `Field { name, type: FieldType.time|number|string|geo|..., values: T[], config, labels, state }`; helpers `getTimeField`, `getFieldDisplayName`, `DataFrameView`, `getFieldMatcher`, `outerJoinDataFrames` (deprecated export of `joinDataFrames`). `transformDataFrame` is RxJS-only. `FieldType.geo` carries OpenLayers geometries (Grafana's convention; not needed by us).
- **Datasource shapes**: SQL `table` → one frame/query, one field/column (best fit: `SELECT time, lat, lon, job_id, zone, ...`). SQL `time_series` → wide frame with `Time` + one numeric field per metric. InfluxDB `time_series` → one `Time`+`Value` frame per series × field; tags as labels → join needed. — [grafana-sql/src/types.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-sql/src/types.ts), [mysql sql_engine.go](https://github.com/grafana/grafana-mysql-datasource/blob/main/pkg/mysql/sqleng/sql_engine.go), [influxdb response_parser.go](https://github.com/grafana/grafana-influxdb-datasource/blob/main/pkg/influxdb/influxql/buffered/response_parser.go)
- **Errors**: `PanelDataErrorView` from `@grafana/runtime` for "no location fields" states.
- **Theme**: `useTheme2().isDark` re-renders on theme switch; key a `useEffect` on it to swap style/paint (do not read `config.theme2` once at module load). — [ThemeContext.tsx](https://github.com/grafana/grafana/blob/main/packages/grafana-ui/src/themes/ThemeContext.tsx)

## 4. External tile fetches: CSP, CORS, mixed content, proxying

- **Proxying**: only `/api/datasources/proxy/uid/:uid/*` and `/api/plugin-proxy/:pluginId/*` (apps) exist in [pkg/api/api.go](https://github.com/grafana/grafana/blob/main/pkg/api/api.go). `routes` in `plugin.json` is documented "For data source plugins". A panel can reach a private tile server only via an existing datasource's proxy URL or a companion app/datasource plugin. — [plugin.json reference](https://grafana.com/developers/plugin-tools/reference/plugin-json)
- **CSP**: `content_security_policy = false` by default. Template at v13.2.1: `script-src 'self' 'unsafe-eval' 'unsafe-inline' 'strict-dynamic' $NONCE; ... style-src 'self' 'unsafe-inline' blob:; img-src * data:; connect-src 'self' grafana.com ws://$ROOT_PATH wss://$ROOT_PATH; ...` (`main` adds `*.cartocdn.com` to `connect-src`). No `worker-src`/`child-src`/`default-src` → `worker-src` falls back to `script-src`, which lacks `blob:`, so blob workers are blocked (cf. [grafana#98807](https://github.com/grafana/grafana/issues/98807)). `img-src *` does not help MapLibre because it uses `fetch()`. — [conf/defaults.ini](https://raw.githubusercontent.com/grafana/grafana/v13.2.1/conf/defaults.ini), [CSP3 fallback](https://www.w3.org/TR/CSP3/)
  README operator note when CSP is enabled: extend `connect-src` with `cache.kartverket.no tiles.mapterhorn.com` (+ any configured XYZ host); no `worker-src` change needed if the worker is self-hosted; trusted types break MapLibre ([maplibre#7210](https://github.com/maplibre/maplibre-gl-js/issues/7210)).
- **CORS**: MapLibre v6.9.1 `getImage` → `fetch()` (mode defaults to `cors`) → `createImageBitmap`; the `<img>` path is only taken with `refreshExpiredTiles: false` and still sets `crossOrigin='anonymous'`. Cross-origin tile hosts must send `Access-Control-Allow-Origin`. Do not use `credentials: 'include'` or custom headers via `transformRequest` against Kartverket (its OPTIONS handling is non-standard). — [ajax.ts](https://github.com/maplibre/maplibre-gl-js/blob/v6.9.1/src/util/ajax.ts), [image_request.ts](https://github.com/maplibre/maplibre-gl-js/blob/v6.9.1/src/util/image_request.ts)
- **Mixed content**: `fetch()` from an HTTPS page to `http://` is blocked; CORS-enabled `<img>` too. HTTPS-only tile URLs. — [MDN Mixed content](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Mixed_content)

### 4.1 Tile sources (verified live)

| Source | URL / notes | CORS | Licence |
|---|---|---|---|
| Kartverket topo (also `topograatone`, `toporaster`, `sjokartraster`) | `https://cache.kartverket.no/v1/wmts/1.0.0/{layer}/default/webmercator/{z}/{y}/{x}.png` — note `{y}/{x}`; PNG only; `cache-control: max-age=432000`; no key. KVP at `/v1/service?...`. Legacy `opencache.statkart.no` unreachable. | `*` | CC BY 4.0, attribution `© Kartverket` + link. Zoom 12–20 include Geovekst data: display as-is is fine, re-tiling/caching needs permission. — [vilkår](https://www.kartverket.no/api-og-data/vilkar-for-bruk), [capabilities](https://cache.kartverket.no/v1/wmts/1.0.0/WMTSCapabilities.xml) |
| Mapterhorn terrain (Kartverket 1 m DTM inside) | `https://tiles.mapterhorn.com/{z}/{x}/{y}.webp`, `raster-dem`, `encoding: 'terrarium'`, `tileSize: 512`, set `maxzoom: 16` (z17+ 404). Used by MapLibre's own 3D-terrain example. | `*` | Open; `© Mapterhorn` (+ `© Kartverket` via [attribution](https://mapterhorn.com/attribution)). — [mapterhorn repo](https://github.com/mapterhorn/mapterhorn) |
| AWS Terrarium (Mapzen) | `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`, max z15, data frozen 2017. Fallback only. | `*` on GET | Norway data `© Kartverket`. — [joerd attribution](https://github.com/tilezen/joerd/blob/master/docs/attribution.md) |
| Kartverket elevation direct | Only GeoTIFF/WCS/WMS (`hoydedata.no`, `wcs.geonorge.no`); **no terrain-RGB tiles**. Self-generate with `rio-rgbify` if z>16 ever matters. | — | CC BY 4.0 |
| Norge i bilder orthophoto | Requires GeoID account; IP-bound tokens; not usable from a browser. **Drop from scope.** — [geonorge.no/nib](https://www.geonorge.no/nib) | — | Licensed, not open |
| OpenStreetMap | `https://tile.openstreetmap.org/{z}/{x}/{y}.png`; usage policy forbids generic clients/heavy use; blocks are served as **200 PNGs** with `x-blocked` header. | `*` | ODbL, `© OpenStreetMap contributors`. — [tile policy](https://operations.osmfoundation.org/policies/tiles/) |

## 5. MapLibre GL JS specifics

- **Version** 6.9.1 (2026-09-14). v6 is ESM-only (`maplibre-gl.mjs` 148 KB gz + `maplibre-gl-shared.mjs` 146 KB gz + `maplibre-gl-worker.mjs` 6 KB gz + CSS 10 KB gz); UMD and CSP builds dropped; WebGL1 removed, WebGL2 required; `Map` constructor throws `GPUInitializationError` when no context. — [CHANGELOG](https://github.com/maplibre/maplibre-gl-js/blob/main/CHANGELOG.md), [v5→v6 guide](https://github.com/maplibre/maplibre-gl-js/blob/main/docs/guides/v5-to-v6-migration-guide.md)
- **Worker**: resolved from `import.meta.url` or `setWorkerUrl()`; same-origin URL → `new Worker(url, {type:'module'})` (no blob). The worker imports `./maplibre-gl-shared.mjs`, so **both** files must sit side by side in `dist/`. MapLibre's own webpack integration test copies both with `copy-webpack-plugin`. Grafana serves `.mjs` from `dist/` as `text/javascript`. — [webpack test config](https://github.com/maplibre/maplibre-gl-js/blob/main/test/integration/bundler/webpack/webpack.config.js), [pkg/api/plugins.go](https://github.com/grafana/grafana/blob/main/pkg/api/plugins.go)
- **Context loss**: v6 restores style automatically on `webglcontextrestored` (custom layers excluded). Browser context cap ~8: `map.remove()` on unmount is mandatory; `setMaxParallelImageRequests` and the worker pool are module-global (shared across panels). — [map.ts](https://github.com/maplibre/maplibre-gl-js/blob/main/src/ui/map.ts), [#3064](https://github.com/maplibre/maplibre-gl-js/issues/3064)
- **Resize**: `trackResize: true` uses a ResizeObserver; additionally call `map.resize()` in an effect on `[width, height]` (no-op if unchanged).
- **Context attributes** (`antialias`, `preserveDrawingBuffer`) now live under `canvasContextAttributes`.
- **Terrain/3D**: `map.setTerrain({source, exaggeration})`; `raster-dem` encodings `mapbox | terrarium | custom`; `hillshade` and `color-relief` layers; `fill-extrusion` for Zone extrusion; `maxPitch` default 60, example uses 85 (experimental >60); `sky` and `projection` (globe since v5) available. — [style spec v8.json](https://github.com/maplibre/maplibre-style-spec/blob/main/src/reference/v8.json)
- **WMTS/XYZ**: `raster` source `tiles` template supports `{z} {x} {y} {quadkey} {bbox-epsg-3857} {ratio}`; RESTful WMTS and KVP WMTS both map onto it; WMS via `{bbox-epsg-3857}`. — [tile_id.ts](https://github.com/maplibre/maplibre-gl-js/blob/main/src/tile/tile_id.ts)
- **Attribution control** is on by default and renders each source's `attribution` string; MapLibre's own logo is optional.
- **deck.gl**: `@deck.gl/maplibre` 9.4.0 `MapLibreOverlay` interleaved mode shares MapLibre's WebGL2 context (no second canvas). Not needed for v1; `fill-extrusion` + `line` layers cover Trail/Coverage/Boundary.

## 6. Options editors: can they host an interactive map?

- `PanelPlugin.setPanelOptions((builder, context) => ...)` runs lazily on every options-pane render; `builder.addCustomEditor({ id, path, name, editor, settings, showIf, category, defaultValue })` takes any `React.ComponentType<StandardEditorProps<TValue, TSettings, TOptions>>`. `StandardEditorProps = { value, onChange(value?), item, context: { data, replaceVariables, options, eventBus, instanceState, fieldConfig, isOverride } }`. Panel-option `onChange` **merges**; field-config `onChange` replaces. — [PanelPlugin.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/panel/PanelPlugin.ts), [standardFieldConfigEditorRegistry.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/field/standardFieldConfigEditorRegistry.ts), [OptionsUIBuilders.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/utils/OptionsUIBuilders.ts)
- Editors render unconstrained inside a `<Field label>` in a pane of initial width **330 px** (user-draggable, not programmatically widenable). Geomap passes `name: ''` to suppress the label. — [PanelEditorRenderer.tsx](https://github.com/grafana/grafana/blob/main/public/app/features/dashboard-scene/panel-edit/PanelEditorRenderer.tsx), [getVisualizationOptions.tsx](https://github.com/grafana/grafana/blob/main/public/app/features/dashboard/components/PanelEditor/getVisualizationOptions.tsx)
- **Yes, an editor can host a WebGL map**, but 330 px is too narrow for drawing. Two sanctioned patterns:
  1. Button in the pane → `Drawer size="lg"` (75 vw) or `Modal` from `@grafana/ui` containing a second MapLibre map; commit via `onChange(polygons)`; create on open, `remove()` on close (portal mount/unmount). First-party precedent: `ValueMappingsEditor` lazily opening a modal.
  2. **Preferred (Geomap's pattern):** draw directly on the panel map. Panel calls `usePanelContext().onInstanceStateChange({ map, mode, actions })`; the options builder reads `context.instanceState` (short-circuit until the map exists) to show "Set dock origin from map click", "Draw Boundary", "Use current view" buttons that operate on the live map object and write results with `onChange` → `onOptionsChange`. Instance-state pushes trigger a panel re-render, so push coarse, stable objects only. — [GeomapPanel.tsx](https://github.com/grafana/grafana/blob/main/public/app/plugins/panel/geomap/GeomapPanel.tsx), [MapViewEditor.tsx](https://github.com/grafana/grafana/blob/main/public/app/plugins/panel/geomap/editor/MapViewEditor.tsx), [PanelContext.ts](https://github.com/grafana/grafana/blob/main/packages/grafana-ui/src/components/PanelChrome/PanelContext.ts), [PanelOptions.tsx](https://github.com/grafana/grafana/blob/main/public/app/features/dashboard-scene/panel-edit/PanelOptions.tsx)
- `addNestedOptions({ path, category, build, values })` with `values.getContext` lets a sub-builder see a different `instanceState` (Geomap uses it per layer). — [layerEditor.tsx](https://github.com/grafana/grafana/blob/main/public/app/plugins/panel/geomap/editor/layerEditor.tsx)
- Migration: `setMigrationHandler` runs before defaults are applied; `getPanelOptionsWithDefaults` re-fills `undefined` options from `defaultValue` on every change.

## 7. Geomap patterns worth borrowing (and what is not importable)

Geomap (`public/app/plugins/panel/geomap/`) is OpenLayers 10.7 (`ol`, `ol-ext`, `ol-mapbox-style`); its "MapLibre layer" applies a style.json via `ol-mapbox-style`, not MapLibre GL JS. Reusable design:

1. `onInstanceStateChange({ map, layers, selected, actions })` bridge (`utils/utils.ts notifyPanelEditor`).
2. Resize without re-creating the map (`map.updateSize()` ↔ `map.resize()`); diff `options.view` / `options.controls` sub-objects, full re-init only on `PanelEditExitedEvent` or variable-dependent layer config.
3. Layer registry contract `MapLayerRegistryItem { create(map, options, eventBus, theme) => Promise<MapLayerHandler{ init, update?(data), dispose?, legend?, registerOptionsUI?(builder, context) }> }` — public in `@grafana/data` (`geo/layer.ts`) but typed against OL; define our own analogue.
4. Location matching (`public/app/features/geo/utils/location.ts`): default field names `lat|latitude`, `lon|lng|longitude`, `geohash`; modes `auto|coords|geohash|lookup`. App-internal, reimplement (~200 lines). For us: Trail rows carry mower-local `x,y` plus Dock origin → compute lon/lat in the panel.
5. `FrameVectorSource.update(frame)` loop: features carry `{frame, rowIndex}`; map to GeoJSON + `source.setData()`; keep `rowIndex` for tooltips/hover sync.
6. Dimension-config styling `{ fixed } | { field, mode, min, max }` maps naturally onto MapLibre data-driven expressions.
7. Tooltip via `Portal` + `VizTooltipContainer` + `VizTooltipRow` (exported from `@grafana/ui`); `DataHoverView` is app-internal.
8. Disposal order: unsubscribe, clear timers, dispose layers, `map.remove()`.
9. View persistence: Geomap does not write pan/zoom back on `moveend`; it offers "Use current map settings" in the editor and optionally mirrors the extent into a dashboard variable via `locationService.partial`, debounced 500 ms.

Cautionary: Geomap's CARTO basemap started showing "API key required" watermarks ([grafana#131610](https://github.com/grafana/grafana/issues/131610)); never hard-depend on a single third-party tile host without a configurable override.

## 8. Prior art: MapLibre/deck.gl inside Grafana

- **[vaduga/mapgl](https://github.com/vaduga/mapgl)** → catalog `vaduga-mapgl-panel` v2.10.1, community-signed, 275 k downloads, pushed 2026-09-14, `grafanaDependency >=11.6.0`, `maplibre-gl ^6.9.0` + deck.gl 9.4 + `@vis.gl/react-maplibre`. Proves MapLibre v6 + deck.gl works and passes signing. Build trick: copies `maplibre-gl.mjs`/`-worker.mjs`/`-shared.mjs` raw into `dist`, loads MapLibre with a native `import(/* webpackIgnore: true */ url)` to bypass the AMD loader, and passes `workerUrl` as a same-origin file. Uses rspack.
- [floodnet-nyc/grafana-time-series-map](https://github.com/floodnet-nyc/grafana-time-series-map): deck.gl 9.3 + maplibre-gl 5.24 with stock create-plugin webpack (bundled, blob worker → CSP-exposed). Not in catalog.
- [flaminggoat/map-track-3-d](https://github.com/flaminggoat/map-track-3-d) (three.js, signed, 1.38 M downloads) — raw WebGL panels are fine in the catalog.
- Older Mapbox GL v1 panels (woutervh-/grafana-mapbox, world-direct/extrusion-panel-plugin) are abandoned. Popular map panels (TrackMap, Orchestra Cities, Worldmap) are Leaflet.
- Known WebGL pitfalls inside Grafana: [grafana#106054](https://github.com/grafana/grafana/issues/106054) (Geomap WebGL markers `GL_INVALID_OPERATION`), Geomap basemap vanishing on screen share (GPU switch), Safari 26 context-lost-on-construct in MapLibre v6 ([#8195](https://github.com/maplibre/maplibre-gl-js/issues/8195)).

## 9. Unsigned plugins on self-hosted Grafana

- `[plugins] allow_loading_unsigned_plugins = randax-navimow-panel` (comma-separated, **exact id match**, applies only to status `unsigned`; modified signatures never load). Env: `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS`. Plugins dir `[paths] plugins` / `GF_PATHS_PLUGINS` (Docker: `/var/lib/grafana/plugins/<id>/{plugin.json,module.js}`). `plugin_admin_enabled` only affects the catalog UI. — [defaults.ini](https://raw.githubusercontent.com/grafana/grafana/v13.2.1/conf/defaults.ini), [authorizer.go](https://raw.githubusercontent.com/grafana/grafana/v13.2.1/pkg/plugins/manager/signature/authorizer.go)
- `app_mode = development` (`GF_DEFAULT_APP_MODE=development`) bypasses the allowlist entirely; the create-plugin Docker dev image sets both. Keep `production` on the real instance.
- Private signing still exists: `pnpm run sign -- --rootUrls https://grafana.example`, requires `GRAFANA_ACCESS_POLICY_TOKEN`; root URLs must match `root_url`. Grafana Cloud does not load unsigned plugins. — [sign-a-plugin](https://grafana.com/developers/plugin-tools/publish-a-plugin/sign-a-plugin)

## 10. Recommended plugin skeleton

```
navimow-panel/                          # pnpm dlx @grafana/create-plugin@latest --pluginType=panel
├── src/
│   ├── module.ts                       # new PanelPlugin<Options>(MapPanel).setNoPadding()
│   │                                   #   .setPanelOptions(buildOptions).setMigrationHandler(migrate)
│   ├── plugin.json                     # id randax-navimow-panel, grafanaDependency ">=12.4.0"
│   ├── types.ts                        # Options { view, basemap, terrain, dockOrigin, boundary, layers, tooltip }
│   ├── components/
│   │   ├── MapPanel.tsx                # PanelProps → useMap() (create once, remove() on unmount,
│   │   │                               #   resize on [width,height], diff option sub-objects), instanceState push
│   │   ├── Tooltip.tsx                 # Portal + VizTooltipContainer/Row, DataHoverEvent publish/subscribe
│   │   └── Fallback.tsx                # GPUInitializationError / no-location-fields (PanelDataErrorView)
│   ├── map/
│   │   ├── maplibre.ts                 # import 'maplibre-gl/dist/maplibre-gl.css'; setWorkerUrl(__webpack_public_path__+'maplibre-gl-worker.mjs')
│   │   ├── basemaps.ts                 # presets: kartverket topo/graatone/toporaster/sjokart, custom XYZ/WMTS, (OSM w/ warning)
│   │   ├── terrain.ts                  # mapterhorn raster-dem terrarium maxzoom 16; setTerrain/hillshade
│   │   └── layers/{trail,coverage,boundary,job}.ts   # frame → GeoJSON with rowIndex; line / fill / fill-extrusion
│   ├── data/
│   │   ├── frames.ts                   # field matchers (time, x/y or lat/lon, job, zone); outerJoinDataFrames for Influx
│   │   └── georef.ts                   # Dock origin (lat, lon, rotation) → lon/lat for Trail points
│   └── editors/
│       ├── DockOriginEditor.tsx        # addCustomEditor; reads context.instanceState.map, "pick from map click"
│       ├── BoundaryEditor.tsx          # draw on panel map (instanceState) or Drawer size="lg"; GeoJSON in options
│       └── ViewEditor.tsx              # "Use current view"
├── webpack.config.ts                   # webpack-merge over .config/webpack; CopyWebpackPlugin for
│                                       #   maplibre-gl-worker.mjs + maplibre-gl-shared.mjs; optional splitChunks
├── tests/panel.spec.ts                 # @grafana/plugin-e2e
├── provisioning/                       # TestData/Postgres datasource + demo dashboard
└── docker-compose.yaml                 # GRAFANA_VERSION=13.2.1
```

Runtime rules baked into the skeleton: one `maplibregl.Map` per panel, `map.remove()` on unmount; all tile URLs HTTPS with `attribution` set on each source; `replaceVariables` applied to option strings on every render; README operator section for CSP `connect-src` and `allow_loading_unsigned_plugins`.

## 11. Open questions passed back to the map

- Whether to lazy-load MapLibre as an async chunk (saves ~295 KB gz on dashboards where the panel is off-screen) or keep it in `module.js` (simpler; matches vaduga's approach of shipping raw `.mjs` files).
- Multi-map dashboards vs the WebGL context cap (relevant to "multi-mower support" in Not-yet-specified).
- Orthophoto is not openly available; remove from Kartverket layer scope or accept a self-hosted source.
