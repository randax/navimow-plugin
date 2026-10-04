import { createDataFrame, FieldType } from '@grafana/data';
import { toLonLat, type DockOrigin } from './dockOrigin';
import { EMPTY_SCENE, mowerAt, placeTrails, trailScene, TRAIL_COLOURS } from './trail';
import type { Trail, TrailPoint } from './trailFrame';

const ORIGIN: DockOrigin = { lat: 59.964, lon: 10.672, rotation: 20 };
const NOW = Date.UTC(2026, 9, 4, 12);
const MIN = 60_000;

const at = (minutesAgo: number, x: number, y: number, extra: Partial<TrailPoint> = {}): TrailPoint => ({
  time: NOW - minutesAgo * MIN,
  x,
  y,
  ...extra,
});
/** A Trail drawn in one unbroken run. */
const run = (points: TrailPoint[], job?: string): Trail => ({ job, segments: [points] });
const place = (points: TrailPoint[]) => points.map((p) => toLonLat(ORIGIN, p.x, p.y));

describe('placeTrails', () => {
  test('the scene records the Dock origin it was placed with, so the view can follow a change', () => {
    expect(placeTrails([run([at(1, 0, 0)])], ORIGIN).origin).toEqual(ORIGIN);
  });

  test('every point of every Trail is drawn, one feature per Job, placed by the Dock origin', () => {
    const trails = [run([at(60, 0, 0), at(59, 1, 0), at(58, 1, 1)], 'a'), run([at(20, 2, 2), at(19, 3, 3)], 'b')];
    const { lines } = placeTrails(trails, ORIGIN);
    expect(lines.features.map((f) => [f.properties.job, f.geometry.coordinates])).toEqual([
      ['a', [place(trails[0].segments[0])]],
      ['b', [place(trails[1].segments[0])]],
    ]);
  });

  test('a Trail with gaps is one feature whose parts are not joined across them', () => {
    const before = [at(60, 0, 0), at(59, 1, 0)];
    const lone = [at(40, 50, 50)];
    const after = [at(20, 100, 0), at(19, 101, 0)];
    const { lines } = placeTrails([{ job: 'a', segments: [before, lone, after] }], ORIGIN);
    // A lone position between gaps is not a line; it still counts towards the mower and the bounds.
    expect(lines.features).toEqual([
      expect.objectContaining({ geometry: { type: 'MultiLineString', coordinates: [place(before), place(after)] } }),
    ]);
  });

  const job = (id: string, minutesAgo: number): Trail => run([at(minutesAgo, 0, 0), at(minutesAgo - 1, 1, 1)], id);
  const coloursOf = (trails: Trail[]) =>
    Object.fromEntries(placeTrails(trails, ORIGIN).lines.features.map((f) => [f.properties.job, f.properties.colour]));

  test('consecutive Jobs get different colours, cycling once the palette runs out', () => {
    const trails = Array.from({ length: TRAIL_COLOURS.length + 1 }, (_, i) => job(`job-${i + 1}`, 100 - 2 * i));
    const colours = Object.values(coloursOf(trails));
    expect(new Set(colours.slice(0, TRAIL_COLOURS.length)).size).toBe(TRAIL_COLOURS.length);
    expect(colours[TRAIL_COLOURS.length]).toBe(colours[0]);
  });

  test('a Job keeps its colour when an older Job leaves the range', () => {
    const trails = [job('41', 300), job('42', 200), job('43', 100)];
    const { 41: _, ...later } = coloursOf(trails);
    expect(coloursOf(trails.slice(1))).toEqual(later);
  });

  test('positions outside any Job keep one neutral colour, whatever else is in range', () => {
    const outside: Trail = { outsideJob: true, segments: [[at(10, 0, 0), at(9, 1, 0)]] };
    const colour = (trails: Trail[]) =>
      placeTrails(trails, ORIGIN).lines.features.find((f) => f.properties.job === null)?.properties.colour;
    const alone = colour([outside]);
    expect(colour([job('41', 300), job('42', 200), outside])).toBe(alone);
    expect(TRAIL_COLOURS).not.toContain(alone);
  });

  test('a Trail from data without a Job column is still drawn', () => {
    const { lines } = placeTrails([run([at(2, 0, 0), at(1, 5, 0)])], ORIGIN);
    expect(lines.features).toEqual([expect.objectContaining({ properties: { job: null, colour: TRAIL_COLOURS[0] } })]);
  });

  test('a single position is too short for a line but still places the mower', () => {
    const scene = placeTrails([run([at(1, 4, 0)])], ORIGIN);
    expect(scene.lines.features).toEqual([]);
    expect(scene.mower?.position).toEqual(toLonLat(ORIGIN, 4, 0));
  });

  test('the mower is at the latest position across all Jobs, facing its heading', () => {
    const scene = placeTrails(
      [
        run([at(30, 0, 0, { heading: 0 }), at(2, 7, 7, { heading: Math.PI / 2 })], 'a'),
        run([at(20, 1, 1, { heading: 0 })], 'b'),
      ],
      ORIGIN
    );
    expect(scene.mower).toEqual({
      position: toLonLat(ORIGIN, 7, 7),
      bearing: expect.closeTo(290, 10),
      time: NOW - 2 * MIN,
    });
  });

  test('a heading that crosses north is still a compass bearing', () => {
    const scene = placeTrails([run([at(1, 0, 0, { heading: Math.PI / 6 })])], { ...ORIGIN, rotation: 10 });
    expect(scene.mower?.bearing).toBeCloseTo(340, 10);
  });

  test('without a heading the mower has no bearing to show', () => {
    expect(placeTrails([run([at(1, 0, 0)])], ORIGIN).mower?.bearing).toBeUndefined();
  });

  test('the bounds cover every point', () => {
    const north = { ...ORIGIN, rotation: 0 };
    const scene = placeTrails([run([at(3, -10, 0), at(2, 10, 0)]), run([at(1, 0, 30)])], north);
    // With x pointing north, y points west.
    expect(scene.bounds).toEqual([
      [toLonLat(north, 0, 30)[0], toLonLat(north, -10, 0)[1]],
      [ORIGIN.lon, toLonLat(north, 10, 0)[1]],
    ]);
  });

  test("the extent is the Trail's longer side in metres, whatever the rotation", () => {
    const trails = [run([at(3, -10, 0), at(2, 10, 1)]), run([at(1, 0, 6)])];
    expect(placeTrails(trails, ORIGIN).extent).toBe(20);
    expect(placeTrails(trails, { ...ORIGIN, rotation: 45 }).extent).toBe(20);
  });

  test('an empty range draws nothing', () => {
    expect(placeTrails([], ORIGIN)).toEqual(EMPTY_SCENE);
  });
});

describe('mowerAt', () => {
  const last = { position: [10.672, 59.964] as [number, number], bearing: 90, time: NOW - 200 * MIN };

  test('a position is aged against the clock it is given', () => {
    expect(mowerAt(last, NOW)).toEqual({ ...last, stale: true, lastSeen: 'Last seen 3 h ago' });
    expect(mowerAt(last, last.time + MIN)).toEqual({ ...last, stale: false });
  });
});

describe('trailScene', () => {
  const frame = createDataFrame({
    fields: [
      { name: 'time', type: FieldType.time, values: [NOW - 2 * MIN, NOW - MIN] },
      { name: 'x', type: FieldType.number, values: [0, 1] },
      { name: 'y', type: FieldType.number, values: [0, 1] },
    ],
  });

  test('frames become a scene placed by the Dock origin', () => {
    expect(trailScene([frame], { dockOrigin: ORIGIN })).toEqual({
      scene: placeTrails([run([at(2, 0, 0), at(1, 1, 1)])], ORIGIN),
    });
  });

  test('a Trail without a Dock origin asks for one instead of drawing in the wrong place', () => {
    expect(trailScene([frame], {})).toEqual({ problem: expect.stringMatching(/Dock origin/) });
  });

  test('an empty range needs no Dock origin, so the map still shows', () => {
    expect(trailScene([], {})).toEqual({ scene: EMPTY_SCENE });
  });

  test('an empty Trail query beside another query with rows still shows the map', () => {
    const empty = createDataFrame({ fields: ['time', 'x', 'y'].map((name) => ({ name, values: [] })) });
    const zoneProgress = createDataFrame({
      fields: [
        { name: 'zone', values: [1, 2] },
        { name: 'progress', values: [50, 10] },
      ],
    });
    expect(trailScene([empty, zoneProgress], { dockOrigin: ORIGIN })).toEqual({ scene: EMPTY_SCENE });
  });

  test('a frame missing required columns explains itself', () => {
    expect(trailScene([frame], { trailColumns: { y: 'postureY' }, dockOrigin: ORIGIN })).toEqual({
      problem: expect.stringMatching(/"postureY"/),
    });
  });
});
