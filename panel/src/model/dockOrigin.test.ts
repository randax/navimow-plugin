import { headingBearing, resolveDockOrigin, toLocal, toLonLat, type DockOrigin } from './dockOrigin';

// Length of a degree at 60° N on the WGS84 ellipsoid, from the standard series expansions.
const METRES_PER_DEGREE_LAT = 111412.3;
const METRES_PER_DEGREE_LON = 55800.0;

const dock = (rotation: number): DockOrigin => ({ lat: 60, lon: 10, rotation });

/** Where a local point lands, in metres east and north of the dock. */
const eastNorth = (origin: DockOrigin, x: number, y: number) => {
  const [lon, lat] = toLonLat(origin, x, y);
  return { east: (lon - origin.lon) * METRES_PER_DEGREE_LON, north: (lat - origin.lat) * METRES_PER_DEGREE_LAT };
};

describe('toLonLat', () => {
  test('the local origin is the dock', () => {
    expect(toLonLat(dock(123), 0, 0)).toEqual([10, 60]);
  });

  test('rotation 0 points the mower x-axis north', () => {
    const { east, north } = eastNorth(dock(0), 100, 0);
    expect(east).toBeCloseTo(0, 6);
    expect(north).toBeCloseTo(100, 1);
  });

  test('rotation 90 points the mower x-axis east', () => {
    const { east, north } = eastNorth(dock(90), 100, 0);
    expect(east).toBeCloseTo(100, 1);
    expect(north).toBeCloseTo(0, 6);
  });

  test('the y-axis is a quarter turn counter-clockwise from x, as theta is', () => {
    const { east, north } = eastNorth(dock(0), 0, 100);
    expect(east).toBeCloseTo(-100, 1);
    expect(north).toBeCloseTo(0, 6);
  });

  test('a Trail whose rotation crosses north mirrors across the meridian', () => {
    const trail = [
      [20, 0],
      [20, 5],
      [-3, 12],
    ];
    trail.forEach(([x, y]) => {
      const west = eastNorth(dock(350), x, y);
      const east = eastNorth(dock(10), x, -y);
      expect(west.east).toBeCloseTo(-east.east, 6);
      expect(west.north).toBeCloseTo(east.north, 6);
    });
    expect(eastNorth(dock(350), 20, 0).east).toBeCloseTo(-20 * Math.sin((10 * Math.PI) / 180), 1);
  });

  test.each([
    [370, 10],
    [-10, 350],
    [720, 0],
  ])('rotation %d wraps to %d', (rotation, same) => {
    const [lon, lat] = toLonLat(dock(rotation), 30, -7);
    const [sameLon, sameLat] = toLonLat(dock(same), 30, -7);
    expect(lon).toBeCloseTo(sameLon, 10);
    expect(lat).toBeCloseTo(sameLat, 10);
  });
});

describe('toLocal', () => {
  test('a place 100 m north of a dock whose x-axis points east is 100 m along y', () => {
    const [x, y] = toLocal(dock(90), 10, 60 + 100 / METRES_PER_DEGREE_LAT);
    expect(x).toBeCloseTo(0, 6);
    expect(y).toBeCloseTo(100, 1);
  });

  test.each([0, 20, 123, 270, 359.5])('undoes toLonLat at rotation %s', (rotation) => {
    const [x, y] = toLocal(dock(rotation), ...toLonLat(dock(rotation), 31.5, -12.25));
    expect(x).toBeCloseTo(31.5, 6);
    expect(y).toBeCloseTo(-12.25, 6);
  });
});

describe('headingBearing', () => {
  test('a mower heading along its x-axis faces the rotation', () => {
    expect(headingBearing(dock(25), 0)).toBeCloseTo(25, 10);
  });

  test('theta counts counter-clockwise, compass bearings clockwise', () => {
    expect(headingBearing(dock(90), Math.PI / 2)).toBeCloseTo(0, 10);
    expect(headingBearing(dock(90), -Math.PI / 2)).toBeCloseTo(180, 10);
  });

  test('a heading that crosses north stays a compass bearing', () => {
    expect(headingBearing(dock(10), Math.PI / 6)).toBeCloseTo(340, 10);
    expect(headingBearing(dock(350), -Math.PI / 6)).toBeCloseTo(20, 10);
    expect(headingBearing(dock(-10), 0)).toBeCloseTo(350, 10);
  });
});

describe('resolveDockOrigin', () => {
  test('rotation defaults to 0', () => {
    expect(resolveDockOrigin({ lat: 59.9, lon: 10.7 })).toEqual({ origin: { lat: 59.9, lon: 10.7, rotation: 0 } });
  });

  test.each([undefined, {}, { lat: 59.9 }, { lon: 10.7, rotation: 20 }, JSON.parse('{"lat":null,"lon":null}')])(
    'a Dock origin without a position (%j) asks for one',
    (options) => {
      expect(resolveDockOrigin(options)).toEqual({ problem: expect.stringMatching(/latitude and longitude/) });
    }
  );

  test.each([
    { lat: 91, lon: 10 },
    { lat: 59.9, lon: 181 },
    { lat: 59.9, lon: 10, rotation: Number.NaN },
  ])('an impossible Dock origin (%j) is rejected', (options) => {
    expect(resolveDockOrigin(options)).toHaveProperty('problem');
  });
});
