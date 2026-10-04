import { readFileSync } from 'node:fs';
import path from 'node:path';
import { toLonLat, type DockOrigin } from './dockOrigin';
import { framedBounds, nextFraming, type Framing } from './framing';
import { EMPTY_SCENE, placeTrails, type Box } from './trail';
import type { TrailPoint } from './trailFrame';

const ORIGIN: DockOrigin = { lat: 59.964, lon: 10.672, rotation: 20 };

// The real Trail of 2026-09-21, in time order: docked, two off-dock runs, docked again.
const FIXTURE: TrailPoint[] = readFileSync(path.join(__dirname, '../../../fixtures/trail-2026-09-21.csv'), 'utf8')
  .trim()
  .split('\n')
  .slice(1)
  .map((line) => line.split(',').map(Number))
  .map(([time, x, y]) => ({ time, x, y }))
  .sort((a, b) => a.time - b.time);
// Its opening stretch: the mower in its dock for half an hour, jittering by about 20 cm.
const DOCKED = FIXTURE.slice(0, 9);

const scene = (points: TrailPoint[], origin = ORIGIN) => placeTrails([{ segments: [points] }], origin);

const contains = ([[minX, minY], [maxX, maxY]]: Box, [[x0, y0], [x1, y1]]: Box) =>
  minX <= x0 && minY <= y0 && maxX >= x1 && maxY >= y1;

/** Replays the Trail growing `step` rows at a time, as refreshes would, and returns every framing made. */
const replay = (points: TrailPoint[], step = 1, origin = ORIGIN) => {
  const framings: Framing[] = [];
  let framed: Framing | undefined;
  for (let n = step; n < points.length + step; n += step) {
    const sofar = scene(points.slice(0, n), origin);
    const next = nextFraming(sofar, framed);
    if (next) {
      framings.push(next);
      framed = next;
    }
    // Whatever happened, the Trail so far is in view.
    expect(contains(framed!.box, sofar.localBox!)).toBe(true);
  }
  return framings;
};

describe('nextFraming', () => {
  test('nothing to draw frames nothing', () => {
    expect(nextFraming(EMPTY_SCENE, undefined)).toBeUndefined();
  });

  test('a docked mower is framed once, with its surroundings, and its jitter never moves the view', () => {
    const framings = replay(DOCKED);
    expect(framings).toHaveLength(1);
    const [[minX, minY], [maxX, maxY]] = framings[0].box;
    expect(maxX - minX).toBeGreaterThanOrEqual(80);
    expect(maxY - minY).toBeGreaterThanOrEqual(80);
  });

  test('the whole real Job, about 48 by 58 m, stays inside the view framed while it was docked', () => {
    const framings = replay(FIXTURE, 10);
    expect(framings).toHaveLength(1);
    expect(contains(framings[0].box, scene(FIXTURE).localBox!)).toBe(true);
  });

  test('a Trail that leaves the view is framed again, once, around all of it', () => {
    const excursion = [70, 80, 90].map((x, i) => ({ time: FIXTURE.at(-1)!.time + (i + 1) * 2000, x, y: 0 }));
    const framings = replay([...FIXTURE, ...excursion], 10);
    expect(framings).toHaveLength(2);
    expect(contains(framings[1].box, scene([...FIXTURE, ...excursion]).localBox!)).toBe(true);
  });

  test.each([
    ['docked', DOCKED],
    ['mowing', FIXTURE],
  ])('turning the Trail about the dock never frames, %s', (_, points) => {
    const framed = nextFraming(scene(points), undefined);
    expect(nextFraming(scene(points, { ...ORIGIN, rotation: 200 }), framed)).toBeUndefined();
  });

  test.each([
    ['latitude', { lat: 59.974 }],
    ['longitude', { lon: 10.682 }],
  ])('moving the dock in %s frames again', (_, moved) => {
    const framed = nextFraming(scene(DOCKED), undefined);
    expect(nextFraming(scene(DOCKED, { ...ORIGIN, ...moved }), framed)).toBeDefined();
  });
});

describe('framedBounds', () => {
  test('cover every point of the framed Trail on the map, at the current rotation', () => {
    const framing = nextFraming(scene(FIXTURE), undefined)!;
    const [[west, south], [east, north]] = framedBounds(framing, ORIGIN);
    FIXTURE.map((p) => toLonLat(ORIGIN, p.x, p.y)).forEach(([lon, lat]) => {
      expect(lon).toBeGreaterThanOrEqual(west);
      expect(lon).toBeLessThanOrEqual(east);
      expect(lat).toBeGreaterThanOrEqual(south);
      expect(lat).toBeLessThanOrEqual(north);
    });
  });
});
