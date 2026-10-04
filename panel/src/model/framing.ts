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

const pad = ([[minX, minY], [maxX, maxY]]: Box): Box => {
  const [cx, cy] = [(minX + maxX) / 2, (minY + maxY) / 2];
  const [hx, hy] = [
    Math.max((maxX - minX) / 2, MIN_HALF_SIDE_METRES),
    Math.max((maxY - minY) / 2, MIN_HALF_SIDE_METRES),
  ];
  return [
    [cx - hx, cy - hy],
    [cx + hx, cy + hy],
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
