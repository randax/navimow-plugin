import type { CoverageSettings } from './coverage';
import { coverageScene } from './coverageScene';
import { toLocal, type DockOrigin } from './dockOrigin';
import type { Trail, TrailPoint } from './trailFrame';

const ORIGIN: DockOrigin = { lat: 59.964, lon: 10.672, rotation: 20 };
const START = Date.UTC(2026, 8, 30, 10);
const SEC = 1000;

const at = (seconds: number, x: number, y: number): TrailPoint => ({ time: START + seconds * SEC, x, y });
/** A Trail drawn in one unbroken run. */
const run = (points: TrailPoint[], job?: string): Trail => ({ job, segments: [points] });

describe('coverageScene', () => {
  const GRID: CoverageSettings = {
    style: 'grid',
    raised: false,
    encoding: 'visits',
    cellSize: 0.5,
    cuttingWidth: 0.43,
  };
  // Out along the row that starts at the dock and, half a minute later, a metre of the way back.
  const out = [at(0, 0.25, 0.25), at(2, 1.25, 0.25), at(4, 2.25, 0.25)];
  const back = [at(34, 2.25, 0.25), at(36, 1.25, 0.25)];
  const trails = [run([...out, ...back])];
  /** A drawn shape's corners, back in the mower's metres, to the millimetre. */
  const local = (ring: number[][]) =>
    ring.map(([lon, lat]) => toLocal(ORIGIN, lon, lat).map((m) => Math.round(m * 1000) / 1000 + 0));
  const scene = (settings: Partial<CoverageSettings> = {}, of: Trail[] = trails) => {
    const drawn = coverageScene(of, ORIGIN, { ...GRID, ...settings });
    if (!drawn) {
      throw new Error('expected Coverage to draw');
    }
    return drawn;
  };

  test('with no Trail, or one that cut nothing, there is no Coverage to draw', () => {
    expect(coverageScene([], ORIGIN, GRID)).toBeUndefined();
    expect(coverageScene([run([at(0, 1, 1)])], ORIGIN, GRID)).toBeUndefined();
  });

  describe('as a Grid', () => {
    test('each cut cell is a square placed by the Dock origin, filled by how often it was cut', () => {
      const { data, layer } = scene();
      expect(layer.type).toBe('fill');
      expect(data.features).toHaveLength(5);
      const first = data.features.find((f) => f.properties.visits === 1);
      expect(first?.geometry.type === 'Polygon' && local(first.geometry.coordinates[0])).toEqual([
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
        [0, 0],
      ]);
      // The way back went over the far three cells again.
      expect(data.features.map((f) => f.properties.visits).sort()).toEqual([1, 1, 2, 2, 2]);
    });

    test('raised, each cell is a column', () => {
      expect(scene({ raised: true }).layer.type).toBe('fill-extrusion');
    });

    test('the same cells tell how long before the latest position each was last cut', () => {
      const { data, legend } = scene({ encoding: 'age' });
      const minutes = data.features.map((f) => f.properties.age ?? NaN).sort((a, b) => a - b);
      // The mower stopped over the third cell, 36 s after it set out from the first. The fourth and
      // fifth were last cut on the way back, a second or two before it stopped; the second on the
      // way out, like the first.
      expect(minutes[0]).toBe(0);
      expect(minutes[4]).toBeCloseTo(36 / 60, 5);
      expect(minutes.map((age) => age < 3 / 60)).toEqual([true, true, true, false, false]);
      expect(legend).toMatchObject({ title: 'Last mowed', ends: ['Latest', '1 min earlier'] });
    });
  });

  describe('as a Heatmap', () => {
    test('each cut cell is a point at its middle, weighing as much as it was cut', () => {
      const { data, layer, note } = scene({ style: 'heatmap' });
      expect(layer.type).toBe('heatmap');
      const points = data.features.map((f) => [
        ...(f.geometry.type === 'Point' ? local([f.geometry.coordinates])[0] : []),
        f.properties.visits,
      ]);
      expect(points.sort()).toEqual([
        [0.25, 0.25, 1],
        [0.75, 0.25, 1],
        [1.25, 0.25, 2],
        [1.75, 0.25, 2],
        [2.25, 0.25, 2],
      ]);
      expect(note).toBeUndefined();
    });

    test('raised, it is the grid with its counts smoothed into the cells around', () => {
      const { data, layer } = scene({ style: 'heatmap', raised: true });
      expect(layer.type).toBe('fill-extrusion');
      // Smoothing spreads the five cut cells into their neighbours, and takes the top off them.
      expect(data.features.length).toBeGreaterThan(5);
      expect(data.features.every((f) => f.geometry.type === 'Polygon')).toBe(true);
      const heights = data.features.map((f) => f.properties.visits ?? NaN);
      expect(Math.max(...heights)).toBeLessThan(2);
      expect(Math.min(...heights)).toBeGreaterThan(0);
      // Along the row the counts 1, 1, 2, 2, 2 smooth to 5/3 over the middle cell of the three cut
      // twice, and across it to a third of that: the rows either side were never cut.
      const middle = data.features.find(
        (f) => f.geometry.type === 'Polygon' && local(f.geometry.coordinates[0])[0].join() === '1.5,0'
      );
      expect(middle?.properties.visits).toBeCloseTo(5 / 9, 9);
    });

    test('it has no way to show time since mowed, and says so while showing visits', () => {
      const flat = scene({ style: 'heatmap', encoding: 'age' });
      expect(flat.note).toBe(
        'A Heatmap shows how often each part was cut, not how long ago. Switch Coverage to Grid to see time since mowed.'
      );
      expect(flat.legend.title).toBe('Visits');
      expect(scene({ style: 'heatmap', encoding: 'age', raised: true }).note).toBe(flat.note);
    });
  });

  describe('as a Buffered line', () => {
    // Two passes with two minutes between them, and a mower that then reports from where it stopped.
    const first = [at(0, 0, 0), at(2, 1, 0), at(4, 1, 1)];
    const second = [at(124, 5, 5), at(126, 6, 5)];
    const parked = [at(426, 6, 5), at(726, 6, 5)];
    const passes = [run([...first, ...second, ...parked])];

    test('the Trail is drawn as wide as the deck, one line whose parts are the unbroken passes', () => {
      const { data, layer, note } = scene({ style: 'line' }, passes);
      expect(layer.type).toBe('line');
      expect(data.features).toHaveLength(1);
      const [{ geometry }] = data.features;
      expect(geometry.type === 'MultiLineString' && geometry.coordinates.map(local)).toEqual([
        first.map((p) => [p.x, p.y]),
        second.map((p) => [p.x, p.y]),
      ]);
      expect(note).toBeUndefined();
    });

    test('for time since mowed, each step is a line of its own, oldest first so the newest shows', () => {
      const { data, legend } = scene({ style: 'line', encoding: 'age' }, passes);
      expect(data.features.map((f) => [f.geometry.type, f.properties.age])).toEqual([
        ['LineString', 124 / 60],
        ['LineString', 122 / 60],
        ['LineString', 0],
      ]);
      expect(legend.title).toBe('Last mowed');
    });

    test('raised, each step is a slab as wide as the deck, run on by half a width so corners close', () => {
      const { data, layer, note } = scene({ style: 'line', raised: true, encoding: 'age', cuttingWidth: 0.4 }, passes);
      expect(layer.type).toBe('fill-extrusion');
      expect(data.features).toHaveLength(3);
      const [slab] = data.features;
      expect(slab.geometry.type === 'Polygon' && local(slab.geometry.coordinates[0])).toEqual([
        [-0.2, -0.2],
        [1.2, -0.2],
        [1.2, 0.2],
        [-0.2, 0.2],
        [-0.2, -0.2],
      ]);
      expect(note).toBeUndefined();
    });

    test('raised, it has no visit count to show, and says so', () => {
      expect(scene({ style: 'line', raised: true }, passes).note).toBe(
        'Raised, a Buffered line shows where the mower has cut, not how often. Switch Coverage to Grid to see visit count.'
      );
    });

    test('beyond 20,000 steps it draws the latest ones, and says how many it left out', () => {
      // 20,003 positions two seconds apart, a centimetre at a time: 20,002 steps.
      const long = [run(Array.from({ length: 20_003 }, (_, i) => at(2 * i, i / 100, 0)))];
      const { data, note } = scene({ style: 'line', encoding: 'age' }, long);
      expect(data.features).toHaveLength(20_000);
      // The oldest step drawn is the third: it ended four seconds in.
      expect(data.features[0].properties.age).toBeCloseTo((2 * 20_002 - 6) / 60, 5);
      expect(note).toBe(
        'A Buffered line draws the latest 20,000 of the 20,002 steps in this time range. Switch Coverage to Grid or Heatmap to see them all.'
      );
      // By visit count the latest steps are still one unbroken line. Drawn apart they would overlap
      // at every position, and each overlap would read as ground cut twice.
      const [{ geometry }] = scene({ style: 'line' }, long).data.features;
      expect(geometry.type === 'MultiLineString' && geometry.coordinates.map((part) => part.length)).toEqual([20_001]);
    });
  });
});
