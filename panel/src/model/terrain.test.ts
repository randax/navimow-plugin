import { resolveTerrain, type TerrainOptions } from './terrain';

describe('resolveTerrain', () => {
  test('Terrain is off unless enabled, whatever else is saved', () => {
    expect(resolveTerrain(undefined)).toEqual({});
    expect(resolveTerrain({ preset: 'aws-terrarium' })).toEqual({});
  });

  test('once enabled it defaults to Mapterhorn, the higher-resolution of the two Presets', () => {
    expect(resolveTerrain({ enabled: true })).toEqual({
      source: {
        type: 'raster-dem',
        tiles: ['https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'],
        encoding: 'terrarium',
        tileSize: 512,
        maxzoom: 16,
        attribution: '<a href="https://mapterhorn.com/attribution">© Mapterhorn</a>',
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

    test('is refused when the URL is not a tile template', () => {
      expect(custom({ url: 'https://terrain.example.com/tiles' })).toEqual({
        problem: expect.stringContaining('A custom Terrain needs an http(s) URL'),
      });
    });

    test('custom attribution is shown as text, never interpreted as markup', () => {
      expect(custom({ attribution: '<b>Me</b>' })).toMatchObject({ source: { attribution: '&lt;b&gt;Me&lt;/b&gt;' } });
    });

    test('a broken custom slot is no problem while Terrain is off', () => {
      expect(resolveTerrain({ enabled: false, preset: 'custom', custom: {} })).toEqual({});
    });
  });
});
