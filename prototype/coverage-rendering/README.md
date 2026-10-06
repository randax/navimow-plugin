# Prototype: Coverage rendering from a real Trail (throwaway)

Wayfinder ticket #15. Open `index.html` in a browser (double-click; needs internet for Kartverket tiles and the MapLibre CDN). Switch variants with the yellow bar, the arrow keys, or `?variant=A|B|C`. Every control in the sidebar is also a URL parameter, so any view is a link: `?variant=B&mode=3d&metric=age&cell=0.25`.

Question: which of three Coverage renderings reads best, flat and extruded, and what must the collector precompute versus what the panel can compute itself?

The Trail is the real Job in `fixtures/raw-2026-09-30.jsonl` (X420, six Zones, one charging break): 3,939 positions about 2 s and 0.75 m apart, 2.6 km of mowing. `extract.py` copies only time, x, y and vehicleState into `trail.js`. It is drawn at the panel's placeholder Dock origin, not in the real garden, on MapLibre 6.11.2, the version the panel ships.

- **A. Heatmap.** Flat: MapLibre's heatmap layer over the raw positions, radius held in metres. Extruded: MapLibre cannot extrude a heatmap, so the grid's counts are blurred and raised instead.
- **B. Visited-cell grid.** Square cells on the mower's own axes. One aggregation gives each cell a visit count and a time last visited; flat it is a fill, extruded each cell is a column.
- **C. Buffered line.** Flat: the Trail drawn as wide as the cutting deck. Extruded: one rectangle per step of the Trail, raised into a slab.

Controls: flat or extruded, visit count or time since mowed, cell size, cutting width, how cells are counted (cutting width swept along the line, centre line only, raw points only), only positions recorded while cutting, terrain, and 1, 10 or 50 Jobs in range (the same Job repeated a day apart and nudged, to stand in for a long time range). The State box shows feature count, GeoJSON size and timings after every change.

## What it showed

Measured in headless Chromium with software rendering, so "drawn after" is pessimistic; compute times are plain JavaScript.

| | 1 Job | 50 Jobs (192k positions) |
|---|---|---|
| B grid, 0.5 m | 2,428 cells, 0.7 MB, aggregate 4 ms | 2,738 cells, 0.8 MB, aggregate 54 ms |
| B grid, 0.25 m | 9,248 cells, 2.8 MB, aggregate 5 ms | 10,522 cells, 3.2 MB, aggregate 165 ms |
| B grid, 0.1 m | 57,932 cells, 17.6 MB | not tried |
| A heatmap, flat | 3,837 points, 0.4 MB | 191,850 points, 21.7 MB, drawn after 1.9 s |
| C line, flat, visit count | 1 feature, 0.1 MB | 1 feature, 7.3 MB |
| C line, flat, time since mowed | 3,835 features, 0.7 MB | 191,750 features, 34 MB, drawn after 3.2 s |
| C line, extruded | 3,835 features, 1.3 MB | 191,750 features, 63 MB, drawn after 4.8 s |

- **Only the grid carries both encodings in both forms.** A heatmap sums, so it cannot show time since mowed, and it has no extruded form of its own. A buffered line shows where passes cross only as darker blending when flat, and has no visit count at all once extruded: counting overlaps means rasterising the passes, which is the grid.
- **The grid's size follows the lawn, not the time range.** Fifty Jobs add 13% more cells than one. The heatmap and the per-step line grow with every position.
- **Aggregation is cheap enough to do in the panel.** 4 ms for a Job, 54 ms for fifty. The grid is in the mower's local metres, so it does not depend on the Dock origin and recalibrating only reprojects it.
- **Cells must be counted along the line, not from raw positions.** Positions are 0.75 m apart, so binning them alone leaves a grid full of holes at 0.25 m (`screenshots/B-raw-points.png`) and still misses a fifth of the cells at 0.5 m. That rules out a plain `GROUP BY` on rounded x and y in the dashboard query.
- **Cutting width barely matters.** Lanes overlap, so the swept area moves only from 510 m² at 0.3 m to 611 m² at 0.6 m; at the X420's 0.43 m it is 579 m², against 652.94 m² reported by the mower.
- **Cell size:** 0.5 m reads cleanly flat; 0.25 m shows lane-pattern speckle flat but is the better-looking surface extruded; 0.1 m is too heavy.
- Extruded cells draw correctly over 3D terrain.
