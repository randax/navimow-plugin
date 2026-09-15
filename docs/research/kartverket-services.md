# Kartverket map, terrain and orthophoto services for MapLibre GL

Research for [issue #2](https://github.com/randax/navimow-plugin/issues/2), 2026-09-15.
Question: which Kartverket services can a browser-side MapLibre GL panel use, and under what terms?

All HTTP/CORS observations below were made with `curl` on 2026-09-15 from a Norwegian residential IP, sending an `Origin` header (`http://localhost:3000` and `https://example.org`).

## TL;DR

| Layer | Service | Browser-usable? | Licence | Attribution |
|---|---|---|---|---|
| Topographic base map | `cache.kartverket.no` WMTS (`topo`, `topograatone`, `toporaster`) | Yes. Plain XYZ-style PNG URL, `Access-Control-Allow-Origin: *`, no key | CC BY 4.0 | `© Kartverket` (link to kartverket.no where possible) |
| 3D terrain (`raster-dem`) | Nothing from Kartverket. Use AWS/Mapzen Terrarium tiles (built from Kartverket DTM 10) or self-convert Kartverket DTM GeoTIFF to Terrain-RGB/Terrarium | AWS tiles: yes, `Access-Control-Allow-Origin: *`, no key | AWS tiles: attribution-only; Kartverket DTM: CC BY 4.0 | `Norway terrain data © Kartverket` (AWS list) / `© Kartverket` |
| Hillshade / slope overlay (2D) | `wms.geonorge.no/skwms1/wms.hoyde-dtm` WMS (`DTM:skyggerelieff`, …) | Yes. CORS reflects Origin, no key | Open data ("Åpne data", no conditions) | `© Kartverket` |
| Orthophoto | Norge i bilder (`wms.nib`, `services.norgeibilder.no`) | No. Token-gated, Norge digitalt parties only | Norge digitalt-lisens (not open) | n/a |

Recommendation: ship `topo`, `topograatone` and `toporaster` as Kartverket presets; use the AWS Terrarium tiles as the default terrain source with a configurable `raster-dem` URL + encoding so a user can point at self-hosted Terrain-RGB/Terrarium tiles built from Kartverket DTM 1 m; offer the DTM hillshade WMS as an optional 2D overlay; do **not** bundle a Norge i bilder orthophoto preset. Let the user paste their own XYZ/WMS orthophoto URL (with token) if they hold a licence.

## 1. Topographic base maps: `cache.kartverket.no` (WMTS)

Source: [cache.kartverket.no](https://cache.kartverket.no/) landing page and the WMTS capabilities documents (fetched 2026-09-15). Kartverket's old `opencache.statkart.no` gatekeeper timed out when probed and should be considered retired; the `v1` service on `cache.kartverket.no` was relaunched in May 2024 ([Geoforum Posisjon 2024-2](https://issuu.com/geoforum/docs/posisjon_2024-2/s/50149335)).

Capabilities:

- REST: `https://cache.kartverket.no/v1/wmts/1.0.0/WMTSCapabilities.xml`
- KVP: `https://cache.kartverket.no/v1/service?service=WMTS&request=GetCapabilities`

Layers (identifier, title from capabilities):

| Identifier | Title | Use |
|---|---|---|
| `topo` | Topo (Fargekart) | Default coloured topographic map |
| `topograatone` | Topo (Gråtone) | Greyscale; good under coloured Trail/Coverage overlays |
| `toporaster` | Topo (Raster) / Turkart | Scanned-look hiking map |
| `sjokartraster` | Sjøkart | Nautical chart; irrelevant for lawns |

TileMatrixSets: `webmercator` (EPSG:3857, matrices `00`–`18`), `utm32n` (EPSG:25832), `utm33n` (EPSG:25833), `utm35n` (EPSG:25835). Format: `image/png` only. Fees: `Ingen`. AccessConstraints: `Ingen`.

Tile URL template (RESTful WMTS; note **row before column**, i.e. `{z}/{y}/{x}`):

```
https://cache.kartverket.no/v1/wmts/1.0.0/{layer}/default/webmercator/{z}/{y}/{x}.png
```

MapLibre `raster` source:

```json
{
  "type": "raster",
  "tiles": ["https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png"],
  "tileSize": 256,
  "maxzoom": 18,
  "attribution": "<a href=\"https://www.kartverket.no/\">© Kartverket</a>"
}
```

Verified 2026-09-15:

- `topo`, `topograatone`, `toporaster` return `200 image/png` at z10–z18 in `webmercator`; z19 returns `400` (max zoom is 18). Lawn-scale work therefore tops out at ~0.3 m/px at 60°N; MapLibre will overzoom the z18 tiles if `maxzoom: 18` is set on the source.
- `Access-Control-Allow-Origin: *` on tiles and on both capabilities documents. `Cache-Control: public, max-age=432000`.
- The `utm33n` matrix set also serves tiles, but MapLibre GL JS only renders EPSG:3857 tile pyramids, so use `webmercator`.

Licence (primary sources: [Kartverket vilkår for bruk](https://www.kartverket.no/api-og-data/vilkar-for-bruk); Geonorge metadata for "Topografisk norgeskart WMTS / cache", uuid `8f381180-1a47-4453-bee7-9a3d64843efa`):

- "Kartverkets gratisprodukt er lisensierte etter Creative Commons Navngivelse 4.0 international (CC BY 4.0)". Geonorge metadata: AccessConstraints `Åpne data`, licence link `https://creativecommons.org/licenses/by/4.0/`, UseLimitations `Ingen begrensninger`.
- Required attribution: "© Kartverket. Det skal også linkast til nettsidene våre der det er mogleg." So the panel must render `© Kartverket` linked to `https://www.kartverket.no/`. The `cache.kartverket.no` landing page itself states the attribution as "© Kartverket".
- Zoom 12–20 caveat, verbatim: "WMS- og cache-tenestene viser på eit visst zoom-nivå (nivå 12-20) data henta frå Geovekst-samarbeidet som Kartverket ikkje har rettane til eller alle rettar til åleine. Dette er data som kan brukast som dei er i ulike tenester på same premiss som for øvrige data. Det må imidlertid innhentast særskild løyve frå rettshavarane dersom dataa skal kopierast eller brukast på annan måte." Consequence for the spec: displaying the tiles live in the panel is fine; the plugin must not bulk-download, cache offline, or redistribute tiles at z12+.
- No published rate limit. Be a good citizen: standard browser tile caching, no prefetch beyond the viewport.

Vector tiles: Kartverket has an experimental vector-tile service (`https://cache.kartverket.no/test/vectortiles/landtopo/{z}/{x}/{y}.mvt`, style `https://vectortiles.kartverket.no/styles/v1/landtopo/style.json`, per [kartverket/kartverket.vectortiles](https://github.com/kartverket/kartverket.vectortiles)). Both URLs returned 404/no response on 2026-09-15 and the repo labels it a test service. Not usable as a preset today.

## 2. Terrain for MapLibre `terrain` / `raster-dem`

### What MapLibre needs

[MapLibre style spec, sources](https://maplibre.org/maplibre-style-spec/sources/#raster-dem): a `raster-dem` source with `encoding` = `"mapbox"` (Terrain-RGB, default), `"terrarium"`, or `"custom"` (`redFactor`, `greenFactor`, `blueFactor`, `baseShift`). `tileSize` defaults to 512. The root [`terrain`](https://maplibre.org/maplibre-style-spec/terrain/) property takes `source` (a raster-dem source) and `exaggeration` (default 1). Terrain is supported in MapLibre GL JS ≥ 2.2.0. A `hillshade` layer can be driven from the same source.

Encodings: Terrain-RGB `h = -10000 + (R·256² + G·256 + B)·0.1`; Terrarium `h = (R·256 + G + B/256) − 32768`.

### Does Kartverket publish DEM tiles?

No. Checked on 2026-09-15:

- The `cache.kartverket.no` WMTS exposes only the four raster map layers above; no DEM layer.
- Kartverket's elevation page ([kartverket.no/en/api-and-data/terrengdata](https://www.kartverket.no/en/api-and-data/terrengdata)) lists point clouds (LAZ), DTM/DOM as GeoTIFF grids, and WMS/WCS/WFS APIs. No tile pyramid, no Terrain-RGB/Terrarium.
- [hoydedata.no](https://hoydedata.no/LaserInnsyn2/) offers a viewer, per-project download and "ready-made exports of nationwide terrain models with a resolution of 10 meters" (GeoTIFF, 7-zip, UTM 33, 50×50 km sheets; Geonorge register [DTM 10 Terrengmodell (UTM33)](https://register.geonorge.no/det-offentlige-kartgrunnlaget/dtm-10-terrengmodell-utm33/dddbb667-1303-4ac5-8640-7ec04c0e3918)). It is a SPA; no documented public tile API.

What does exist (all under Kartverket, no key, CORS enabled):

| Service | URL | Notes |
|---|---|---|
| Nasjonal høydemodell DTM WCS (EPSG:25833) | `https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833?service=WCS&request=GetCapabilities&version=2.0.1` (also reachable on `wms.geonorge.no`) | CoverageId `nhm_dtm_topo_25833`; DescribeCoverage shows a **1 m** grid (`offsetVector 1 0 / 0 -1`), 1 250 529 × 1 600 549 cells, formats `image/tiff`, `image/netcdf`. UTM 32/35 variants: `wcs.hoyde-dtm-nhm-25832`, `-25835`. |
| Seamless laser DTM WCS | `https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm_somlos` | CoverageIds `las_dtm`, `las_dtm_dynamisk_farget_hoyde`; GeoTIFF/NetCDF. `Access-Control-Allow-Origin: *`. |
| DTM WMS (visualisation) | `https://wms.geonorge.no/skwms1/wms.hoyde-dtm?service=WMS&request=GetCapabilities` | Layers `DTM`, `DTM:skyggerelieff`, `DTM:multiskyggerelieff`, `DTM:dynamisk_farget_hoyde`, `DTM:helning_grader`, `DTM:helning_prosent`, `DTM:helning_grader_jord_nve`. GetMap in EPSG:3857 returned `200 image/png` with CORS reflecting the request Origin (`Access-Control-Allow-Credentials: true`). Usable as a MapLibre `raster` source via a WMS `{bbox-epsg-3857}` template for a 2D hillshade overlay. |

Licence: Geonorge metadata for the NHM DTM WCS (uuid `0f0a0f38-00c4-4213-a9e5-2d861dc4abb0`) is `Åpne data`, "No conditions apply to access and use"; DTM 10 (uuid `dddbb667-…`) is CC BY 4.0. Both fall under Kartverket's general CC BY 4.0 terms with `© Kartverket` attribution. WCS capabilities state no fees/constraints.

### Option A (recommended default): AWS/Mapzen Terrarium tiles

The Mapzen/Tilezen "Terrain Tiles" on the AWS Open Data registry ([registry.opendata.aws/terrain-tiles](https://registry.opendata.aws/terrain-tiles/), bucket `s3://elevation-tiles-prod/`, EU replica `elevation-tiles-prod-eu`) are a public Terrarium pyramid that, per [tilezen/joerd data-sources.md](https://github.com/tilezen/joerd/blob/master/docs/data-sources.md), uses "Kartverket's Digital Terrain Model, 10 meters over Norway" at zoom ≥ 10.

```
https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
```

Verified 2026-09-15: `200 image/png`, `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET`. No API key.

MapLibre:

```json
"sources": {
  "terrain": {
    "type": "raster-dem",
    "tiles": ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
    "encoding": "terrarium",
    "tileSize": 256,
    "maxzoom": 15,
    "attribution": "Norway terrain data © Kartverket"
  }
},
"terrain": { "source": "terrain", "exaggeration": 1 }
```

Attribution required ([joerd attribution.md](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)): "Norway terrain data © Kartverket" (plus the other providers' lines if the map is used outside Norway; for a Norway-only preset the Kartverket line suffices, but the generic global line "…GMTED2010 and SRTM terrain data courtesy of the U.S. Geological Survey" applies at low zooms). Caveats: 10 m source resolution and no SLA (the registry says "New data is added based on community feedback"). For a garden this is coarse but adequate for draping a Trail; a lawn on a slope will look right, micro-relief will not.

### Option B: self-hosted Terrain-RGB/Terrarium from Kartverket DTM 1 m

Needed when 10 m is too coarse. There is no Kartverket tile pyramid, so a conversion step is required:

1. Fetch the DTM GeoTIFF for the lawn's bounding box from the WCS above (`GetCoverage` with `subset=E(...)`/`subset=N(...)`, `format=image/tiff`), or download the DTM 1 m/10 m sheets from hoydedata.no.
2. Reproject to EPSG:3857 (`gdalwarp -t_srs EPSG:3857 -r bilinear`).
3. Encode to Terrain-RGB tiles with [mapbox/rio-rgbify](https://github.com/mapbox/rio-rgbify): `rio rgbify -b -10000 -i 0.1 --min-z 8 --max-z 17 --format png dtm_3857.tif dtm.mbtiles` (`-b`/`--base-val` −10000 and `-i`/`--interval` 0.1 reproduce the `mapbox` encoding). rio-rgbify is low-activity upstream (17 open issues, ~125 stars), but it is the reference implementation.
4. Serve the MBTiles (or convert to PMTiles with `pmtiles convert` and serve statically; MapLibre reads PMTiles via the `pmtiles` protocol handler) and use `encoding: "mapbox"`.

Licence for this path: the derived tiles are a CC BY 4.0 derivative; attribute `© Kartverket` and link. The zoom 12–20 Geovekst caveat applies to the map cache, not to the national elevation model, which is Kartverket's own open dataset.

The plugin should expose the `raster-dem` URL template, `encoding` and `tileSize` as panel options with Option A pre-filled, so Option B needs no code change.

## 3. Orthophoto: Norge i bilder

Primary sources: [Kartverket vilkår for bruk](https://www.kartverket.no/api-og-data/vilkar-for-bruk); Geonorge metadata "Norge i bilder WMS-Ortofoto" (uuid `dcee8bf4-fdf3-4433-a91b-209c7d9b0b0f`); [geonorge.no/nib](https://www.geonorge.no/nib) migration notice; [Norge digitalt-lisens](https://www.kartverket.no/geodataarbeid/norge-digitalt/partsinformasjon/avtaler-og-vilkar/norge-digitalt-lisens); live probes.

Findings:

- Licence: Geonorge metadata says AccessConstraints `Norge digitalt begrenset`, UseConstraints `Lisens`, licence = Norge digitalt-lisens. Kartverket's terms: "Alle flybilete på norgeskart.no og norgeibilder.no er lisensierte produkt". The only free use granted is screenshots: "Det er likevel fritt å bruke skjermbilete, eller skjermdump, av både flybilete og kart frå dei to nettsidene mot å kreditere kjelda: ©norgeskart.no eller ©norgeibilder.no."
- Access: the legacy WMS `https://wms.geonorge.no/skwms1/wms.nib` answers GetCapabilities with `Access-Control-Allow-Origin: *`, but an anonymous GetMap returns a ServiceException: "Bruker kan ikke autentiseres … TCP/IP adresse ikke godkjent av autorisasjons tjener" (IP allow-list). The new endpoints `https://services.norgeibilder.no/wms/ortofoto`, `/wms/prosjekter`, `/wms/mosaikk` and WMTS on `tilecache.norgeibilder.no` require a token from `https://services.norgeibilder.no/token`, issued against GeoID credentials, valid 1 h/1 d/1 week and **bound to an IP address**; "Access to the service is granted to Norge digitalt parties and those who have entered into data access agreements." The old `wms.geonorge.no` services are scheduled to close by 30 September 2026.
- Rights: imagery is owned by Geovekst parties/Omløpsfoto, not Kartverket alone. The Norge digitalt licence requires source attribution and deletion of copies when a task is complete; it is a party-to-party licence, not a public one. The [OSM permission thread](https://community.openstreetmap.org/t/tillatelse-til-a-bruke-norge-i-bilder/82988) shows Kartverket granting OSM a narrow permission to trace, with the explicit note that the service "kan bare brukes av OSM til mapping, ikke f.eks. som bakgrunnslag i andre tjenester".

Conclusion: Norge i bilder orthophotos are **not** open data and cannot be shipped as a working preset in a published open-source plugin. No API key exists that a plugin author could legitimately embed, and per-user tokens are IP-bound and reserved for Norge digitalt parties. The spec should treat orthophoto as a user-supplied raster layer: an optional "custom XYZ/WMS URL" slot where a licensed user (e.g. a municipal employee who is a Norge digitalt party) can paste a tokenised URL. The plugin must not hard-code any `norgeibilder.no` URL or suggest that use is licensed.

## 4. CORS summary (2026-09-15)

| Endpoint | `Access-Control-Allow-Origin` |
|---|---|
| `cache.kartverket.no/v1/wmts/1.0.0/{layer}/default/webmercator/{z}/{y}/{x}.png` | `*` |
| `cache.kartverket.no/v1/service?…GetCapabilities` and `…/WMTSCapabilities.xml` | `*` |
| `wms.geonorge.no/skwms1/wms.hoyde-dtm` (GetCapabilities and GetMap) | reflects request Origin, `Allow-Credentials: true` |
| `wms.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833` | reflects request Origin |
| `wcs.geonorge.no/skwms1/wcs.hoyde-dtm_somlos` | `*` |
| `wms.geonorge.no/skwms1/wms.nib` | `*` (but GetMap refused without IP authorisation) |
| `services.norgeibilder.no/wms/ortofoto` | reflects Origin (ArcGIS Server; `499` without token) |
| `s3.amazonaws.com/elevation-tiles-prod/terrarium/…` | `*` |

Grafana panels run in the browser at the Grafana origin, so all the open endpoints above work without a proxy.

## 5. Recommended layer set for the spec

1. **Base map presets (Norway):** `Kartverket topo` (default), `Kartverket topo gråtone`, `Kartverket turkart (toporaster)`. Source type `raster`, tileSize 256, maxzoom 18, attribution `<a href="https://www.kartverket.no/">© Kartverket</a>`.
2. **Terrain:** `raster-dem` preset "Kartverket DTM 10 via AWS Terrarium" (`encoding: terrarium`, attribution `Norway terrain data © Kartverket`), with URL/encoding/tileSize editable so a self-hosted Terrain-RGB derived from Kartverket DTM 1 m can replace it. `hillshade` layer optional, same source.
3. **Optional 2D hillshade overlay:** Kartverket `wms.hoyde-dtm` layer `DTM:skyggerelieff` as a raster WMS source, attribution `© Kartverket`.
4. **Orthophoto:** no bundled preset. Generic "custom raster URL" option for users with their own licence.
5. **Outside Norway:** OSM default, per the standing decision in #1.

Licence constraints the spec must respect:

- Always show `© Kartverket` (linked) when any Kartverket layer is visible; show `Norway terrain data © Kartverket` when the AWS terrain is active.
- Display-only use of `cache.kartverket.no` tiles: no bulk download, offline caching or redistribution (Geovekst content at z12–z20).
- No Norge i bilder URLs, tokens or presets in the shipped plugin.
- Kartverket max zoom is 18; overzoom client-side rather than requesting z19+.
