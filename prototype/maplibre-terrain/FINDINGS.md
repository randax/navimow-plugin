# Prototype findings: MapLibre terrain inside a Grafana panel

Throwaway spike for wayfinder ticket #14. Not production code.

Run it: `pnpm install && pnpm build && GRAFANA_VERSION=13.2.1 docker compose up -d`, open
http://localhost:3000/d/terrain-3d. `node probe.mjs <outdir>` screenshots both dashboards
headless and collects console/network results. CSP variants: add `-f docker-compose.csp.yaml`
(stock CSP, breaks tiles) or `-f docker-compose.csp-fixed.yaml` (extended connect-src, works).

## Setup
- create-plugin 7.10.1, pnpm 10.25, Node 24; Grafana 13.2.1 (enterprise image, unsigned plugin allowed).
- maplibre-gl 6.10.0 bundled into module.js; worker + shared `.mjs` copied to `dist/` by a root
  `webpack.config.ts` merged over the scaffold config; `setWorkerUrl(__webpack_public_path__ + ...)`.
- Style: Kartverket topo WMTS (`{z}/{y}/{x}`) raster, Mapterhorn terrarium raster-dem (tileSize 512,
  maxzoom 16) used for both `terrain` and a `hillshade` layer, GeoJSON Trail (30 lanes), dock and
  mower circles. Options: terrain on/off, exaggeration, pitch.

## Results
| Measure | Value |
|---|---|
| `dist/module.js` | 1.16 MB raw, 295 KB gzip (MapLibre + CSS is nearly all of it) |
| Extra files in dist | `maplibre-gl-shared.mjs` 509 KB, `maplibre-gl-worker.mjs` 19 KB |
| typecheck | pass |
| Tiles, 3D dashboard | Kartverket 52-63 × 200, Mapterhorn 14-20 × 200, no 4xx/5xx, no CORS errors |
| Tiles, 2D dashboard | Kartverket 9 × 200, Mapterhorn 2 × 200 (hillshade only) |
| First `idle`, 3D | 7.3-9.9 s under SwiftShader software GL in headless Chromium; expect far less on a GPU |
| First `idle`, 2D | 0.9 s |
| Terrain in 3D | Yes. Relief visible, Trail draped on the slope, pitch 60 and bearing 20 honoured, attribution reads `© Mapterhorn, © Kartverket | © Kartverket | MapLibre` |
| Plugin console errors | None. Only MapLibre's advice to use separate sources for hillshade and terrain, and SwiftShader GPU-stall warnings |
| Stock Grafana CSP on | All tile fetches blocked by `connect-src`; worker still loads (same-origin). Map renders empty |
| CSP with `connect-src` + `https://cache.kartverket.no https://tiles.mapterhorn.com` | Everything works, zero CSP violations |

## Gotchas found
1. Enabling terrain *after* style load (`map.setTerrain`) keeps the camera altitude, so the view
   jumps out by the ground elevation (~370 m here) and the Trail becomes a dot. Put `terrain` in the
   initial style when the option starts on; toggling later needs a zoom correction.
2. copy-webpack-plugin `from` paths are resolved against `src/` (webpack context), so use absolute paths.
3. `$NONCE` / `$ROOT_PATH` in a docker-compose env value must be written `$$NONCE` / `$$ROOT_PATH`.
4. `@types/geojson` is needed for GeoJSON typings.

## Screenshots
- `screenshots/terrain-3d.png`, `screenshots/terrain-2d.png`
