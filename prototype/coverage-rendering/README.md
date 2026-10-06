# Prototype: Coverage rendering from a real Trail (throwaway)

Wayfinder ticket #15. From the repository root, run `python3 -m http.server 8015 -d prototype/coverage-rendering` and open <http://localhost:8015/> (needs internet for Kartverket tiles and the MapLibre CDN). Double-clicking `index.html` does not work: MapLibre 6 runs its worker as a module, which browsers refuse to start for a page opened as a file, so the map appears with no Coverage on it. Switch variants with the yellow bar, the arrow keys, or `?variant=A|B|C`. Every control in the sidebar is also a URL parameter, so any view is a link: `?variant=B&mode=3d&metric=age&cell=0.25`.

Question: which of three Coverage renderings reads best, flat and extruded, and what must the collector precompute versus what the panel can compute itself?

The Trail is the real Job captured on 2026-09-30 (X420, six Zones, one charging break): 3,939 positions about 2 s and 0.75 m apart, 2.6 km of mowing. `extract.py` copies only time, x, y and vehicleState into `trail.js`, from either the committed `fixtures/job-2026-09-30.jsonl.gz` or its unredacted original `fixtures/raw-2026-09-30.jsonl`; both give the same positions. It is drawn at the panel's placeholder Dock origin, not in the real garden, on MapLibre 6.11.2, the version the panel ships.

- **A. Heatmap.** Flat: MapLibre's heatmap layer over the raw positions, radius held in metres. Extruded: MapLibre cannot extrude a heatmap, so the grid's counts are blurred and raised instead.
- **B. Visited-cell grid.** Square cells on the mower's own axes. One aggregation gives each cell a visit count and a time last visited; flat it is a fill, extruded each cell is a column.
- **C. Buffered line.** Flat: the Trail drawn as wide as the cutting deck. Extruded: one rectangle per step of the Trail, raised into a slab.

Controls: flat or extruded, visit count or time since mowed, cell size, cutting width, how cells are counted (cutting width swept along the line, centre line only, raw points only), only positions recorded while cutting, terrain, and 1, 10 or 50 Jobs in range. More Jobs are the same Job repeated a day apart and nudged by up to 0.2 m: a load test for the arithmetic and the drawing, not a picture of a real season. The State box shows feature count, GeoJSON size and timings after every change.

## What it showed

Measured in headless Chromium with software rendering, so "drawn after" is pessimistic; compute times are plain JavaScript.

| | 1 Job | 50 Jobs (192k positions) |
|---|---|---|
| B grid, 0.5 m | 2,428 cells, 0.7 MB, aggregate 4 ms | 2,738 cells, 0.8 MB, aggregate 55 ms |
| B grid, 0.25 m | 9,248 cells, 2.8 MB, aggregate 5 ms | 10,522 cells, 3.2 MB, aggregate 170 ms |
| B grid, 0.1 m | 57,932 cells, 17.6 MB | not tried |
| A heatmap, flat | 3,837 points, 0.4 MB | 191,850 points, 21.7 MB, drawn after about 2 s |
| C line, flat, visit count | 1 feature, 0.1 MB | 1 feature, 7.3 MB |
| C line, flat, time since mowed | 3,835 features, 0.7 MB | 191,750 features, 34 MB, drawn after 3 to 4 s |
| C line, extruded | 3,835 features, 1.3 MB | 191,750 features, 63 MB, drawn after 4 to 5 s |

- **Only the grid carries both encodings in both forms.** A heatmap sums, so it cannot show time since mowed, and it has no extruded form of its own. A buffered line shows where passes cross only as darker blending when flat, and has no visit count at all once extruded: counting overlaps means rasterising the passes, which is the grid.
- **What the map has to draw.** A grid can never hold more cells than the lawn has area, whatever the time range, while the heatmap and the per-step line carry one feature per position. The 50-Job column shows the second half of that (192k features, tens of megabytes); it cannot show the first, because the repeated Job covers the same ground by construction.
- **Aggregation is cheap.** About 4 ms for this Job and 55 ms for 192k positions at 0.5 m. The grid is in the mower's local metres, so it does not depend on the Dock origin and recalibrating only reprojects it.
- **Cells must be counted along the line, not from raw positions.** Positions are 0.75 m apart, so binning them alone leaves a grid full of holes at 0.25 m (`screenshots/B-raw-points.png`) and still misses a fifth of the cells at 0.5 m. A `GROUP BY` on rounded x and y over the stored positions is therefore not enough; consecutive positions have to be joined into lines first.
- **Cutting width is a second-order input.** At 0.5 m cells it changes nothing: a 0.43 m deck never reaches a neighbouring cell's centre, so sweeping the width marks the same 2,428 cells as the centre line alone. At 0.25 m it does matter (9,248 cells swept, 7,789 from the centre line). Rasterised at 5 cm, the swept area is 510 m² at 0.3 m, 579 m² at the X420's 0.43 m and 611 m² at 0.6 m, against 652.94 m² reported by the mower: doubling the width adds a fifth, because lanes overlap.
- **Cell size:** 0.5 m reads cleanly flat; 0.25 m shows lane-pattern speckle flat but is the better-looking surface extruded; 0.1 m is too heavy.
- Extruded cells draw correctly over 3D terrain.

## Recommendation

Render Coverage as **B, the visited-cell grid**, computed **in the panel** from the Trail frame it already receives, with nothing precomputed by the collector and nothing added to the schema. Count cells along the line between consecutive positions; default the cell to 0.5 m and offer it as a panel option.

Not measured here: what it costs to fetch a long time range of positions through a Grafana datasource. The panel fetches those positions anyway to draw the Trail, so Coverage adds no query of its own, but if that fetch proves too slow the remedy (thinning or pre-aggregating in the collector) would change this answer. That is the map's open item on long Trails, and this prototype does not settle it.
