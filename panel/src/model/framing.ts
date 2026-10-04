import type { TrailScene } from './trail';

/** What the view was last framed on: the dock's position, and whether the Trail had any extent. */
export interface Framing {
  origin: string;
  wide: boolean;
}

// Well above a docked mower's jitter (about 20 cm over half an hour in the 2026-09-21 fixture, and
// up to a metre over a morning, #4) and well below any mowing.
const WIDE_METRES = 5;

/**
 * Whether the view should frame the scene, and if so what it is then framed on; undefined to leave
 * the view alone. A Trail is framed when it first appears and when the dock moves, but never on a
 * refresh, which must not undo the owner's panning, nor on a rotation, which turns the Trail about
 * the dock while the owner compares it with the lawn. While it is no more than a docked mower, each
 * refresh frames it again, so the Job that follows is framed once it sets off.
 */
export function nextFraming(scene: TrailScene, previous: Framing | undefined): Framing | undefined {
  if (!scene.bounds || !scene.origin) {
    return undefined;
  }
  const next = { origin: `${scene.origin.lat},${scene.origin.lon}`, wide: (scene.extent ?? 0) >= WIDE_METRES };
  return !previous || previous.origin !== next.origin || !previous.wide ? next : undefined;
}
