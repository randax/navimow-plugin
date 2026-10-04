import { resolveTerrain, type TerrainOptions } from './terrain';

describe('resolveTerrain', () => {
  test('Terrain is off unless enabled, whatever else is saved', () => {
    expect(resolveTerrain(undefined)).toEqual({});
    expect(resolveTerrain(null)).toEqual({});
    expect(resolveTerrain({ preset: 'aws-terrarium' })).toEqual({});
  });

  test.each(['false', 'true', 1, null])('only true enables it: saved as %p it stays off', (enabled) => {
    expect(resolveTerrain({ enabled } as unknown as TerrainOptions)).toEqual({});
  });

  test('a preset saved as null means the default one', () => {
    expect(resolveTerrain({ enabled: true, preset: null } as unknown as TerrainOptions)).toEqual(
      resolveTerrain({ enabled: true })
    );
  });

  test('once enabled it defaults to Mapterhorn, the higher-resolution of the two Presets', () => {
    expect(resolveTerrain({ enabled: true })).toEqual({
      source: {
        type: 'raster-dem',
        tiles: ['https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'],
        encoding: 'terrarium',
        tileSize: 512,
        maxzoom: 16,
        // Kartverket is credited in its own right, whatever the Base map: its data is what is shown.
        attribution:
          '<a href="https://mapterhorn.com/attribution">© Mapterhorn</a>, <a href="https://www.kartverket.no/">© Kartverket</a>',
      },
    });
  });

  test('AWS Terrain Tiles are the coarser Preset, stopping a zoom level earlier', () => {
    expect(resolveTerrain({ enabled: true, preset: 'aws-terrarium' })).toEqual({
      source: {
        type: 'raster-dem',
        tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
        encoding: 'terrarium',
        tileSize: 256,
        maxzoom: 15,
        attribution: 'Norway terrain data © Kartverket',
      },
    });
  });

  test('a preset id this version does not know is refused rather than crashing the panel', () => {
    expect(resolveTerrain({ enabled: true, preset: 'retired' } as unknown as TerrainOptions)).toEqual({
      problem: 'Unknown Terrain "retired". Choose another under Terrain in the panel options.',
    });
  });

  describe('custom', () => {
    const url = 'https://terrain.example.com/{z}/{x}/{y}.png';
    const custom = (fields: object) =>
      resolveTerrain({ enabled: true, preset: 'custom', custom: { url, attribution: '© Me', ...fields } });

    test('a URL with its encoding, tile size, max zoom and attribution becomes a raster-dem source', () => {
      expect(custom({ encoding: 'mapbox', tileSize: 256, maxzoom: 14, attribution: '© Example' })).toEqual({
        source: {
          type: 'raster-dem',
          tiles: [url],
          encoding: 'mapbox',
          tileSize: 256,
          maxzoom: 14,
          attribution: '© Example',
        },
      });
    });

    test('left blank, it is read as the common kind: terrarium tiles of 512 pixels up to zoom 16', () => {
      expect(custom({})).toMatchObject({ source: { encoding: 'terrarium', tileSize: 512, maxzoom: 16 } });
    });

    test('an encoding the map cannot decode is refused, since panel JSON bypasses the editor', () => {
      expect(custom({ encoding: 'geotiff' })).toEqual({
        problem: 'A custom Terrain needs its encoding set to terrarium or mapbox.',
      });
    });

    test('is refused without attribution', () => {
      expect(custom({ attribution: ' ' })).toEqual({
        problem: 'A custom Terrain needs an attribution. Enter the credit line its provider requires.',
      });
    });

    test.each([
      'https://terrain.example.com/tiles',
      // A map service draws pictures of the ground, not tiles that hold its elevation.
      'https://wms.example.com/wms?REQUEST=GetMap&BBOX={bbox-epsg-3857}',
      'https://[invalid]/{z}/{x}/{y}.png',
    ])('is refused when %p is not an http(s) tile template', (template) => {
      expect(custom({ url: template })).toEqual({
        problem: 'A custom Terrain needs an http(s) URL containing {z}, {x} and {y}.',
      });
    });

    test('custom attribution is shown as text, never interpreted as markup', () => {
      expect(custom({ attribution: '<b>Me</b>' })).toMatchObject({ source: { attribution: '&lt;b&gt;Me&lt;/b&gt;' } });
    });

    test.each([null, { url: null, attribution: '© Me' }])(
      'saved as %p it is refused for its URL, not crashed by it',
      (saved) => {
        expect(resolveTerrain({ enabled: true, preset: 'custom', custom: saved } as unknown as TerrainOptions)).toEqual(
          {
            problem: 'A custom Terrain needs an http(s) URL containing {z}, {x} and {y}.',
          }
        );
      }
    );

    test('numbers and an encoding saved as null fall back to the common kind', () => {
      expect(custom({ encoding: null, tileSize: null, maxzoom: '14' })).toMatchObject({
        source: { encoding: 'terrarium', tileSize: 512, maxzoom: 14 },
      });
    });

    test('a broken custom slot is no problem while Terrain is off', () => {
      expect(resolveTerrain({ enabled: false, preset: 'custom', custom: {} })).toEqual({});
    });
  });
});
