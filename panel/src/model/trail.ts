import type { FeatureCollection, LineString } from 'geojson';
import { headingBearing, toLonLat, type DockOrigin } from './dockOrigin';
import { recency, type Recency } from './recency';
import type { Trail } from './trailFrame';

// Strong hues that stand apart from each other and from the greens, whites and water blues of a
// topographic Base map.
export const TRAIL_COLOURS = ['#E02F44', '#1F60C4', '#FF780A', '#8F3BB8', '#D6338E', '#37474F'];

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

/** Places Trails on the map: one coloured line per Job, and the mower where it was last seen. */
export function drawTrails(trails: Trail[], origin: DockOrigin, now: number): TrailScene {
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
                properties: { job: trail.job ?? null, colour: TRAIL_COLOURS[i % TRAIL_COLOURS.length] },
                geometry: { type: 'LineString', coordinates: placed[i] },
              },
            ]
      ),
    },
  };

  const all = placed.flat();
  if (all.length === 0) {
    return scene;
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
    ...(last.heading === undefined ? {} : { bearing: headingBearing(origin, last.heading) }),
    ...recency(last.time, now),
  };
  return scene;
}
