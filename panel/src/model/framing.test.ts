import { readFileSync } from 'node:fs';
import path from 'node:path';
import { toLonLat, type DockOrigin } from './dockOrigin';
import { framedBounds, movesView, nextFraming, type Framing } from './framing';
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

/**
 * Replays the Trail growing `step` points per refresh and returns every framing made. Framing reads
 * only the scene's local box and origin, so the box is kept up as it grows rather than placing the
 * whole Trail again on every refresh.
 */
const replay = (points: TrailPoint[], step = 1, origin = ORIGIN) => {
  const framings: Framing[] = [];
  let framed: Framing | undefined;
  let box: Box = [
    [Infinity, Infinity],
    [-Infinity, -Infinity],
  ];
  points.forEach(({ x, y }, i) => {
    box = [
      [Math.min(box[0][0], x), Math.min(box[0][1], y)],
      [Math.max(box[1][0], x), Math.max(box[1][1], y)],
    ];
    if ((i + 1) % step !== 0 && i !== points.length - 1) {
      return;
    }
    const next = nextFraming({ ...EMPTY_SCENE, localBox: box, origin }, framed);
    if (next) {
      framings.push(next);
      framed = next;
    }
    // Whatever happened, the Trail so far is in view.
    expect(contains(framed!.box, box)).toBe(true);
  });
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
    const framings = replay(FIXTURE);
    expect(framings).toHaveLength(1);
    expect(contains(framings[0].box, scene(FIXTURE).localBox!)).toBe(true);
  });

  test('a Trail that leaves the view is framed again, once, around all of it', () => {
    const excursion = [70, 80, 90].map((x, i) => ({ time: FIXTURE.at(-1)!.time + (i + 1) * 2000, x, y: 0 }));
    const framings = replay([...FIXTURE, ...excursion]);
    expect(framings).toHaveLength(2);
    expect(contains(framings[1].box, scene([...FIXTURE, ...excursion]).localBox!)).toBe(true);
  });

  test('a lawn wider than the view is framed only a handful of times as it is mowed', () => {
    // 100 by 100 m, mowed in 50 stripes 2 m apart, one refresh per stripe.
    const stripes = Array.from({ length: 50 }, (_, i) => [
      { time: i * 2, x: 0, y: 2 * i },
      { time: i * 2 + 1, x: 100, y: 2 * i },
    ]).flat();
    expect(replay(stripes, 2).length).toBeLessThanOrEqual(5);
  });

  test('the same Trail refreshed again never frames again, whatever its decimals', () => {
    const same = {
      ...EMPTY_SCENE,
      origin: ORIGIN,
      localBox: [
        [0.123, 0],
        [80.456, 5],
      ] as Box,
    };
    const framed = nextFraming(same, undefined);
    for (let i = 0; i < 10; i++) {
      expect(nextFraming(same, framed)).toBeUndefined();
    }
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

describe('movesView', () => {
  const docked = nextFraming(scene(DOCKED), undefined)!;
  const grown = nextFraming(scene(FIXTURE), undefined)!;
  const moved = nextFraming(scene(DOCKED, { ...ORIGIN, lat: 59.974 }), undefined)!;

  test('a view the owner steers is moved by every framing', () => {
    expect(movesView(docked, undefined, false)).toBe(true);
    expect(movesView(grown, docked, false)).toBe(true);
  });

  test('a view that follows the mower is not thrown out to the whole Trail each time the Trail grows', () => {
    expect(movesView(grown, docked, true)).toBe(false);
  });

  test('a following view is still framed when the Trail first appears, and when the dock moves', () => {
    expect(movesView(docked, undefined, true)).toBe(true);
    expect(movesView(moved, docked, true)).toBe(true);
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
