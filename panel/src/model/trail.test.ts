import { toLonLat, type DockOrigin } from './dockOrigin';
import { drawTrails, TRAIL_COLOURS } from './trail';
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

describe('drawTrails', () => {
  test('every point of every Trail is drawn, one line per Job, placed by the Dock origin', () => {
    const trails: Trail[] = [
      { job: 'a', points: [at(60, 0, 0), at(59, 1, 0), at(58, 1, 1)] },
      { job: 'b', points: [at(20, 2, 2), at(19, 3, 3)] },
    ];
    const { lines } = drawTrails(trails, ORIGIN, NOW);
    expect(lines.features).toHaveLength(2);
    expect(lines.features.map((f) => f.properties.job)).toEqual(['a', 'b']);
    expect(lines.features[0].geometry.coordinates).toEqual(trails[0].points.map((p) => toLonLat(ORIGIN, p.x, p.y)));
    expect(lines.features[1].geometry.coordinates).toHaveLength(2);
  });

  test('each Job gets its own colour, cycling once the palette runs out', () => {
    const trails = Array.from({ length: TRAIL_COLOURS.length + 1 }, (_, i) => ({
      job: `job-${i}`,
      points: [at(100 - i, 0, 0), at(99 - i, 1, 1)],
    }));
    const colours = drawTrails(trails, ORIGIN, NOW).lines.features.map((f) => f.properties.colour);
    expect(new Set(colours.slice(0, TRAIL_COLOURS.length)).size).toBe(TRAIL_COLOURS.length);
    expect(colours[TRAIL_COLOURS.length]).toBe(colours[0]);
  });

  test('a Trail from data without a Job column is still drawn', () => {
    const { lines } = drawTrails([{ points: [at(2, 0, 0), at(1, 5, 0)] }], ORIGIN, NOW);
    expect(lines.features).toEqual([expect.objectContaining({ properties: { job: null, colour: TRAIL_COLOURS[0] } })]);
  });

  test('a single position is too short for a line but still places the mower', () => {
    const scene = drawTrails([{ points: [at(1, 4, 0)] }], ORIGIN, NOW);
    expect(scene.lines.features).toEqual([]);
    expect(scene.mower?.position).toEqual(toLonLat(ORIGIN, 4, 0));
  });

  test('the mower is at the latest position across all Jobs, facing its heading', () => {
    const scene = drawTrails(
      [
        { job: 'a', points: [at(30, 0, 0, { heading: 0 }), at(2, 7, 7, { heading: Math.PI / 2 })] },
        { job: 'b', points: [at(20, 1, 1, { heading: 0 })] },
      ],
      ORIGIN,
      NOW
    );
    expect(scene.mower).toEqual({ position: toLonLat(ORIGIN, 7, 7), bearing: expect.closeTo(290, 10), stale: false });
  });

  test('a heading that crosses north is still a compass bearing', () => {
    const scene = drawTrails([{ points: [at(1, 0, 0, { heading: Math.PI / 6 })] }], { ...ORIGIN, rotation: 10 }, NOW);
    expect(scene.mower?.bearing).toBeCloseTo(340, 10);
  });

  test('without a heading the mower has no bearing to show', () => {
    expect(drawTrails([{ points: [at(1, 0, 0)] }], ORIGIN, NOW).mower).not.toHaveProperty('bearing');
  });

  test('an old position is marked stale with its age', () => {
    const scene = drawTrails([{ points: [at(200, 0, 0, { heading: 0 })] }], ORIGIN, NOW);
    expect(scene.mower).toMatchObject({ stale: true, lastSeen: 'Last seen 3 h ago' });
  });

  test('the bounds cover every point', () => {
    const north = { ...ORIGIN, rotation: 0 };
    const scene = drawTrails([{ points: [at(3, -10, 0), at(2, 10, 0)] }, { points: [at(1, 0, 30)] }], north, NOW);
    // With x pointing north, y points west.
    expect(scene.bounds).toEqual([
      [toLonLat(north, 0, 30)[0], toLonLat(north, -10, 0)[1]],
      [ORIGIN.lon, toLonLat(north, 10, 0)[1]],
    ]);
  });

  test('an empty range draws nothing', () => {
    expect(drawTrails([], ORIGIN, NOW)).toEqual({ lines: { type: 'FeatureCollection', features: [] } });
  });
});
