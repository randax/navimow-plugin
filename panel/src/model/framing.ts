import { toLonLat, type DockOrigin } from './dockOrigin';
import type { Box, TrailScene } from './trail';

/** What the view was last framed on: the dock's position, and a box on the mower's own axes. */
export interface Framing {
  dock: string;
  box: Box;
}

// The least the view shows around the centre of the Trail, so a docked mower is seen with its
// garden rather than at full zoom on a few centimetres of jitter.
const MIN_HALF_SIDE_METRES = 40;
// Room left on each side, as a share of the Trail's size, so a lawn larger than the least view is
// not framed again for every new stripe at its edge.
const MARGIN = 0.25;

/** One axis of the framed box. Grown outward from the Trail's own edges, so it always holds them exactly. */
const padAxis = (min: number, max: number): [number, number] => {
  const centre = (min + max) / 2;
  const half = Math.max((max - min) * (0.5 + MARGIN), MIN_HALF_SIDE_METRES);
  return [Math.min(min, centre - half), Math.max(max, centre + half)];
};

const pad = ([[minX, minY], [maxX, maxY]]: Box): Box => {
  const [[x0, x1], [y0, y1]] = [padAxis(minX, maxX), padAxis(minY, maxY)];
  return [
    [x0, y0],
    [x1, y1],
  ];
};

const contains = ([[minX, minY], [maxX, maxY]]: Box, [[x0, y0], [x1, y1]]: Box) =>
  minX <= x0 && minY <= y0 && maxX >= x1 && maxY >= y1;

/**
 * Whether the view should frame the scene, and if so what it is then framed on; undefined to leave
 * it alone. The view is framed when the Trail first appears, when the dock moves, and when the Trail
 * grows out of what was framed. Nothing else moves it: not a refresh within the framed box, not the
 * owner's panning, and not a rotation, which turns the Trail about the dock while the owner compares
 * it with the lawn. Working on the mower's own axes is what keeps rotation out of it.
 */
export function nextFraming(scene: TrailScene, previous: Framing | undefined): Framing | undefined {
  if (!scene.localBox || !scene.origin) {
    return undefined;
  }
  const dock = `${scene.origin.lat},${scene.origin.lon}`;
  if (previous && previous.dock === dock && contains(previous.box, scene.localBox)) {
    return undefined;
  }
  return { dock, box: pad(scene.localBox) };
}

/**
 * Whether a new framing moves the view. It does, unless the view follows the mower: then the mower
 * holds the centre and the owner the zoom, and a Trail growing out of the framed box is noted
 * without zooming out to it. A Trail's first appearance and a moved dock are framed either way, as
 * there is no view of them to keep.
 */
export const movesView = (next: Framing, previous: Framing | undefined, following: boolean): boolean =>
  !following || previous?.dock !== next.dock;

/** The framed box on the map, as longitude and latitude bounds at the Dock origin's current rotation. */
export function framedBounds({ box: [[minX, minY], [maxX, maxY]] }: Framing, origin: DockOrigin): Box {
  const corners = [
    toLonLat(origin, minX, minY),
    toLonLat(origin, minX, maxY),
    toLonLat(origin, maxX, minY),
    toLonLat(origin, maxX, maxY),
  ];
  const lons = corners.map(([lon]) => lon);
  const lats = corners.map(([, lat]) => lat);
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
}
