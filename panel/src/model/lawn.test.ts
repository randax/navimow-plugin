import type { DockOrigin } from './dockOrigin';
import {
  boundaryFeatures,
  handlePosition,
  HANDLE_METRES,
  lawnText,
  migrateLawn,
  nextZoneId,
  parseLawn,
  resolveLawn,
  rotationTowards,
  type Lawn,
} from './lawn';

const dock: DockOrigin = { lat: 60, lon: 10, rotation: 0 };
const square: Array<[number, number]> = [
  [10, 60],
  [10.001, 60],
  [10.001, 60.001],
  [10, 60.001],
];

describe('parseLawn', () => {
  test('reads back exactly what lawnText wrote', () => {
    const lawn: Lawn = {
      dockOrigin: { lat: 60, lon: 10, rotation: 20 },
      boundary: { outline: square, zones: [{ id: '1', name: 'Front', ring: square }] },
    };
    expect(parseLawn(lawnText(lawn))).toEqual({ lawn });
  });

  test('an empty object is a Lawn with nothing in it', () => {
    expect(parseLawn('{}')).toEqual({ lawn: {} });
  });

  test('text that is not JSON says so', () => {
    expect(parseLawn('{"dockOrigin": ')).toEqual({ problem: expect.stringContaining('not valid JSON') });
  });

  test('a Dock origin coordinate that is not a finite number is refused by name', () => {
    expect(parseLawn('{"dockOrigin": {"lat": "60", "lon": 10}}')).toEqual({
      problem: expect.stringContaining('dockOrigin.lat'),
    });
    expect(parseLawn('{"dockOrigin": {"lat": 60, "lon": null}}')).toEqual({
      problem: expect.stringContaining('dockOrigin.lon'),
    });
  });

  test('a Dock origin may be partly set, as the plain fields allow', () => {
    expect(parseLawn('{"dockOrigin": {"rotation": 45}}')).toEqual({ lawn: { dockOrigin: { rotation: 45 } } });
  });

  test('a ring needs at least three [longitude, latitude] corners', () => {
    expect(parseLawn('{"boundary": {"outline": [[10, 60], [10.001, 60]]}}')).toEqual({
      problem: expect.stringContaining('outline'),
    });
    expect(parseLawn('{"boundary": {"outline": [[10, 60], [10.001, 60], [10, "x"]]}}')).toEqual({
      problem: expect.stringContaining('outline'),
    });
    expect(parseLawn('{"boundary": {"outline": [[10, 60], [10.001, 60], [200, 60]]}}')).toEqual({
      problem: expect.stringContaining('outline'),
    });
  });

  test('a closed ring, as GeoJSON writes it, is opened', () => {
    const closed = [...square, square[0]];
    expect(parseLawn(JSON.stringify({ boundary: { outline: closed } }))).toEqual({
      lawn: { boundary: { outline: square } },
    });
  });

  test("a Zone's identifier is kept as text even when pasted as a number, and its name defaults from it", () => {
    expect(parseLawn(JSON.stringify({ boundary: { zones: [{ id: 2, ring: square }] } }))).toEqual({
      lawn: { boundary: { zones: [{ id: '2', name: 'Zone 2', ring: square }] } },
    });
  });

  test('a Zone without a ring is refused by position', () => {
    expect(parseLawn(JSON.stringify({ boundary: { zones: [{ id: '1', name: 'a', ring: square }, { id: '2' }] } }))).toEqual(
      { problem: expect.stringContaining('zones[1]') }
    );
  });

  test('a Zone identifier may be empty while editing, but the ring must be there', () => {
    expect(parseLawn(JSON.stringify({ boundary: { zones: [{ id: '', name: '', ring: square }] } }))).toEqual({
      lawn: { boundary: { zones: [{ id: '', name: '', ring: square }] } },
    });
  });

  test('anything that is not an object is refused', () => {
    expect(parseLawn('[]')).toEqual({ problem: expect.any(String) });
    expect(parseLawn('"lawn"')).toEqual({ problem: expect.any(String) });
    expect(parseLawn('{"boundary": 3}')).toEqual({ problem: expect.stringContaining('boundary') });
  });
});

describe('rotationTowards', () => {
  test('a handle due north of the dock means rotation 0', () => {
    expect(rotationTowards(dock, [10, 60.0005])).toBeCloseTo(0, 3);
  });

  test('a handle due east means 90, south 180, west 270', () => {
    expect(rotationTowards(dock, [10.001, 60])).toBeCloseTo(90, 3);
    expect(rotationTowards(dock, [10, 59.9995])).toBeCloseTo(180, 3);
    expect(rotationTowards(dock, [9.999, 60])).toBeCloseTo(270, 3);
  });

  test('a handle on the dock itself keeps the rotation there is', () => {
    expect(rotationTowards({ ...dock, rotation: 123 }, [10, 60])).toBe(123);
  });

  test('round trips through handlePosition at every rotation', () => {
    for (const rotation of [0, 1, 89.5, 180, 270, 359.9]) {
      const origin = { ...dock, rotation };
      expect(rotationTowards(origin, handlePosition(origin))).toBeCloseTo(rotation, 4);
    }
  });
});

describe('handlePosition', () => {
  test('sits HANDLE_METRES along the x-axis', () => {
    const [lon, lat] = handlePosition({ ...dock, rotation: 90 });
    expect(lat).toBeCloseTo(60, 6);
    expect((lon - 10) * 55800).toBeCloseTo(HANDLE_METRES, 0);
  });
});

describe('boundaryFeatures', () => {
  test('nothing drawn without a Boundary', () => {
    expect(boundaryFeatures(undefined).features).toEqual([]);
    expect(boundaryFeatures({}).features).toEqual([]);
  });

  test('the outline and each Zone become closed polygons that name themselves', () => {
    const { features } = boundaryFeatures({ outline: square, zones: [{ id: '3', name: 'Back', ring: square }] });
    expect(features.map((f) => f.properties)).toEqual([
      { kind: 'outline', label: 'Outline' },
      { kind: 'zone', label: 'Back (3)', id: '3' },
    ]);
    for (const f of features) {
      expect(f.geometry.coordinates[0]).toEqual([...square, square[0]]);
    }
  });

  test('a Zone named like its identifier, or not at all, is labelled once', () => {
    const { features } = boundaryFeatures({
      zones: [
        { id: '3', name: '3', ring: square },
        { id: '4', name: '', ring: square },
      ],
    });
    expect(features.map((f) => f.properties.label)).toEqual(['3', '4']);
  });
  test('a Zone carries its latest progress, which colours it; a Zone with none reported is drawn as before', () => {
    const zones = ['3', '4'].map((id) => ({ id, name: '', ring: square }));
    const { features } = boundaryFeatures({ outline: square, zones }, { '3': { progress: 64 }, '9': { progress: 5 } });
    expect(features.map((f) => f.properties)).toEqual([
      { kind: 'outline', label: 'Outline' },
      { kind: 'zone', label: '3', id: '3', progress: 64 },
      { kind: 'zone', label: '4', id: '4' },
    ]);
  });
});

describe('nextZoneId', () => {
  test('counts on from the largest numeric identifier in use', () => {
    expect(nextZoneId([])).toBe('1');
    expect(nextZoneId([{ id: '1', name: '', ring: square }, { id: '7', name: '', ring: square }])).toBe('8');
    expect(nextZoneId([{ id: 'patio', name: '', ring: square }])).toBe('1');
  });
});

describe('resolveLawn', () => {
  const origin = { lat: 60, lon: 10, rotation: 20 };

  test('reads the Lawn saved for any mower, which is where a panel for one mower keeps it', () => {
    const lawn = { dockOrigin: origin, boundary: { outline: square } };
    expect(resolveLawn({ lawns: { '*': lawn } })).toEqual(lawn);
  });

  test('a Lawn saved for a named mower belongs to a panel for several, and is not this panel’s', () => {
    expect(resolveLawn({ lawns: { 'mower-1': { dockOrigin: origin } } })).toBeUndefined();
  });

  test('reads a Lawn saved under lawn, or a Dock origin at the root, as the migration handler may not have run', () => {
    expect(resolveLawn({ lawn: { dockOrigin: origin, boundary: { outline: square } } })).toEqual({
      dockOrigin: origin,
      boundary: { outline: square },
    });
    expect(resolveLawn({ dockOrigin: origin })).toEqual({ dockOrigin: origin });
  });

  test('prefers the newest place each value is saved in, and keeps what only an older place has', () => {
    expect(
      resolveLawn({
        lawns: { '*': { dockOrigin: { lat: 1, lon: 2 } } },
        lawn: { dockOrigin: { lat: 3, lon: 4 }, boundary: { outline: square } },
        dockOrigin: origin,
      })
    ).toEqual({ dockOrigin: { lat: 1, lon: 2, rotation: 20 }, boundary: { outline: square } });
    expect(resolveLawn({ lawn: { boundary: { outline: square } }, dockOrigin: origin })).toEqual({
      boundary: { outline: square },
      dockOrigin: origin,
    });
  });

  test('a plain field edited before the panel is migrated changes that value and keeps the rest', () => {
    expect(resolveLawn({ lawns: { '*': { dockOrigin: { lat: 61 } } }, lawn: { dockOrigin: origin } })).toEqual({
      dockOrigin: { lat: 61, lon: 10, rotation: 20 },
    });
  });

  test('is nothing for a panel with none of them', () => {
    expect(resolveLawn({})).toBeUndefined();
  });
});

describe('migrateLawn', () => {
  test('moves a Dock origin saved at the root to the Lawn for any mower', () => {
    const options = { baseMap: { preset: 'osm' }, dockOrigin: { lat: 60, lon: 10, rotation: 20 } };
    expect(migrateLawn(options)).toEqual({
      baseMap: { preset: 'osm' },
      lawns: { '*': { dockOrigin: { lat: 60, lon: 10, rotation: 20 } } },
    });
  });

  test('moves a Lawn saved under lawn there too, Boundary and all', () => {
    const lawn = { dockOrigin: { lat: 1, lon: 2 }, boundary: { outline: square } };
    expect(migrateLawn({ baseMap: { preset: 'osm' }, lawn })).toEqual({
      baseMap: { preset: 'osm' },
      lawns: { '*': lawn },
    });
  });

  test('leaves options already keyed by mower alone, other mowers included', () => {
    const options = { baseMap: { preset: 'osm' }, lawns: { '*': { dockOrigin: { lat: 1, lon: 2 } }, 'mower-1': {} } };
    expect(migrateLawn(options)).toEqual(options);
    expect(migrateLawn({ baseMap: { preset: 'osm' }, lawn: undefined })).toEqual({ baseMap: { preset: 'osm' } });
  });

  test('an older place never overrides a newer one', () => {
    const options = {
      dockOrigin: { lat: 60, lon: 10 },
      lawn: { dockOrigin: { lat: 3, lon: 4 } },
      lawns: { '*': { dockOrigin: { lat: 1, lon: 2 } }, 'mower-1': { dockOrigin: { lat: 5, lon: 6 } } },
    };
    expect(migrateLawn(options)).toEqual({
      lawns: { '*': { dockOrigin: { lat: 1, lon: 2 } }, 'mower-1': { dockOrigin: { lat: 5, lon: 6 } } },
    });
  });
});
