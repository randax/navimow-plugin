import type { DataFrame } from '@grafana/data';
import type { FeatureCollection, LineString } from 'geojson';
import { headingBearing, resolveDockOrigin, toLonLat, type DockOrigin, type DockOriginOptions } from './dockOrigin';
import { recency, type Recency } from './recency';
import { readTrails, type Trail, type TrailColumns } from './trailFrame';

// Strong hues that stand apart from each other and from the greens, whites and water blues of a
// topographic Base map.
export const TRAIL_COLOURS = ['#E02F44', '#1F60C4', '#FF780A', '#8F3BB8', '#D6338E', '#37474F'];

// A Job's colour comes from its identifier, so it keeps it as older Jobs leave the range. The hash is
// taken modulo the palette at every step, so identifiers that differ by one in their last character,
// as sequential ones do, land on neighbouring colours rather than clashing.
const colourOf = (job: string): string =>
  TRAIL_COLOURS[[...job].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % TRAIL_COLOURS.length, 0)];

export type Bounds = [[number, number], [number, number]];

export type MowerMarker = {
  position: [number, number];
  /** Compass bearing of the mower's heading, when the data has one. */
  bearing?: number;
} & Recency;

/** Everything the map draws for the Trail, already in longitude and latitude. */
export interface TrailScene {
  lines: FeatureCollection<LineString, { job: string | null; colour: string }>;
  mower?: MowerMarker;
  bounds?: Bounds;
}

export const EMPTY_SCENE: TrailScene = { lines: { type: 'FeatureCollection', features: [] } };

/** The panel options the Trail depends on. */
export interface TrailOptions {
  dockOrigin?: DockOriginOptions;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
}

/** Places Trails on the map: one coloured line per Job, and the mower where it was last seen. */
export function placeTrails(trails: Trail[], origin: DockOrigin, now: number): TrailScene {
  const placed = trails.map((t) => t.points.map((p) => toLonLat(origin, p.x, p.y)));
  const scene: TrailScene = {
    lines: {
      type: 'FeatureCollection',
      features: trails.flatMap((trail, i) =>
        // A single position is not a line; it still counts towards the mower and the bounds.
        placed[i].length < 2
          ? []
          : [
              {
                type: 'Feature',
                // A numeric id survives into rendered features, so drawn Trails can be counted across tiles.
                id: i,
                properties: {
                  job: trail.job ?? null,
                  // Without a Job column there is no identifier; such Trails are told apart by position.
                  colour: trail.job === undefined ? TRAIL_COLOURS[i % TRAIL_COLOURS.length] : colourOf(trail.job),
                },
                geometry: { type: 'LineString', coordinates: placed[i] },
              },
            ]
      ),
    },
  };

  const all = placed.flat();
  if (all.length === 0) {
    return EMPTY_SCENE;
  }
  // Reduced rather than spread into Math.min: a week of Trail is more points than a call takes as arguments.
  scene.bounds = all.reduce<Bounds>(
    ([[west, south], [east, north]], [lon, lat]) => [
      [Math.min(west, lon), Math.min(south, lat)],
      [Math.max(east, lon), Math.max(north, lat)],
    ],
    [
      [Infinity, Infinity],
      [-Infinity, -Infinity],
    ]
  );

  const last = trails.flatMap((t) => t.points).reduce((a, b) => (b.time > a.time ? b : a));
  scene.mower = {
    position: toLonLat(origin, last.x, last.y),
    bearing: last.heading === undefined ? undefined : headingBearing(origin, last.heading),
    ...recency(last.time, now),
  };
  return scene;
}

/**
 * The panel's whole Trail pipeline, from query frames and options to what the map draws, or to the
 * problem to show instead. With nothing to draw, a missing Dock origin is not a problem yet.
 * `now` is when the data was fetched; without it, the wall clock (see recency).
 */
export function trailScene(
  frames: DataFrame[],
  { trailColumns, dockOrigin }: TrailOptions,
  now = Date.now()
): { scene: TrailScene } | { problem: string } {
  const read = readTrails(frames, trailColumns);
  if ('problem' in read) {
    return read;
  }
  if (read.trails.length === 0) {
    return { scene: EMPTY_SCENE };
  }
  const resolved = resolveDockOrigin(dockOrigin);
  return 'problem' in resolved ? resolved : { scene: placeTrails(read.trails, resolved.origin, now) };
}
