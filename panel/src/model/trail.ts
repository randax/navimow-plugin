import type { DataFrame } from '@grafana/data';
import type { FeatureCollection, MultiLineString } from 'geojson';
import { headingBearing, resolveDockOrigin, toLonLat, type DockOrigin, type DockOriginOptions } from './dockOrigin';
import { recency, type Recency } from './recency';
import { readTrails, type Trail, type TrailColumns } from './trailFrame';

// Strong hues that stand apart from each other and from the greens, whites and water blues of a
// topographic Base map.
export const TRAIL_COLOURS = ['#E02F44', '#1F60C4', '#FF780A', '#8F3BB8', '#D6338E', '#37474F'];

// Positions outside any Job, such as moving off and onto the dock: grey, so they never pass for a Job.
const OUTSIDE_JOB_COLOUR = '#8E8E8E';

// A Job's colour comes from its identifier, so it keeps it as older Jobs leave the range. The hash is
// taken modulo the palette at every step, so identifiers that differ by one in their last character,
// as sequential ones do, land on neighbouring colours rather than clashing.
const colourOf = (job: string): string =>
  TRAIL_COLOURS[[...job].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % TRAIL_COLOURS.length, 0)];

export type Bounds = [[number, number], [number, number]];

/** Where the mower was last seen, and when. */
export interface LastPosition {
  position: [number, number];
  /** Compass bearing of the mower's heading, when the data has one. */
  bearing?: number;
  time: number;
}

export type MowerMarker = LastPosition & Recency;

/** Everything the map draws for the Trail, already in longitude and latitude. */
export interface TrailScene {
  /** One feature per Trail, its parts the unbroken runs between gaps in the data. */
  lines: FeatureCollection<MultiLineString, { job: string | null; colour: string }>;
  mower?: LastPosition;
  bounds?: Bounds;
  /** The longer side of the Trail's box on the mower's own axes, in metres. */
  extent?: number;
  /** What placed it; a new Dock origin can move the Trail far enough to need framing again. */
  origin?: DockOrigin;
}

export const EMPTY_SCENE: TrailScene = { lines: { type: 'FeatureCollection', features: [] } };

/** The panel options the Trail depends on. */
export interface TrailOptions {
  dockOrigin?: DockOriginOptions;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
}

// Reduced rather than spread into Math.min: a week of Trail is more points than a call takes as arguments.
const box = (points: Array<[number, number]>): Bounds =>
  points.reduce<Bounds>(
    ([[minA, minB], [maxA, maxB]], [a, b]) => [
      [Math.min(minA, a), Math.min(minB, b)],
      [Math.max(maxA, a), Math.max(maxB, b)],
    ],
    [
      [Infinity, Infinity],
      [-Infinity, -Infinity],
    ]
  );

/** Places Trails on the map: one coloured feature per Job, and the mower where it was last seen. */
export function placeTrails(trails: Trail[], origin: DockOrigin): TrailScene {
  const placed = trails.map((t) => t.segments.map((s) => s.map((p) => toLonLat(origin, p.x, p.y))));
  const scene: TrailScene = {
    lines: {
      type: 'FeatureCollection',
      features: trails.flatMap((trail, i) => {
        // A lone position is not a line; it still counts towards the mower and the bounds.
        const parts = placed[i].filter((part) => part.length > 1);
        return parts.length === 0
          ? []
          : [
              {
                type: 'Feature' as const,
                // A numeric id survives into rendered features, so drawn Trails can be counted across tiles.
                id: i,
                properties: {
                  job: trail.job ?? null,
                  // Without a Job column there is no identifier; such Trails are told apart by position.
                  colour: trail.outsideJob
                    ? OUTSIDE_JOB_COLOUR
                    : trail.job === undefined
                      ? TRAIL_COLOURS[i % TRAIL_COLOURS.length]
                      : colourOf(trail.job),
                },
                geometry: { type: 'MultiLineString' as const, coordinates: parts },
              },
            ];
      }),
    },
    origin,
  };

  const all = placed.flat(2);
  if (all.length === 0) {
    return EMPTY_SCENE;
  }
  scene.bounds = box(all);
  const points = trails.flatMap((t) => t.segments.flat());
  const [[minX, minY], [maxX, maxY]] = box(points.map((p) => [p.x, p.y]));
  scene.extent = Math.max(maxX - minX, maxY - minY);

  const last = points.reduce((a, b) => (b.time > a.time ? b : a));
  scene.mower = {
    position: toLonLat(origin, last.x, last.y),
    bearing: last.heading === undefined ? undefined : headingBearing(origin, last.heading),
    time: last.time,
  };
  return scene;
}

/** The mower as shown at `now`: current, or faded with its age. */
export const mowerAt = (last: LastPosition, now: number): MowerMarker => ({ ...last, ...recency(last.time, now) });

/**
 * The panel's whole Trail pipeline, from query frames and options to what the map draws, or to the
 * problem to show instead. With nothing to draw, a missing Dock origin is not a problem yet.
 */
export function trailScene(
  frames: DataFrame[],
  { trailColumns, dockOrigin }: TrailOptions
): { scene: TrailScene } | { problem: string } {
  const read = readTrails(frames, trailColumns);
  if ('problem' in read) {
    return read;
  }
  if (read.trails.length === 0) {
    return { scene: EMPTY_SCENE };
  }
  const resolved = resolveDockOrigin(dockOrigin);
  return 'problem' in resolved ? resolved : { scene: placeTrails(read.trails, resolved.origin) };
}
