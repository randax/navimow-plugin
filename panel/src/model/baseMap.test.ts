import { resolveBaseMap, type BaseMapOptions } from './baseMap';

describe('resolveBaseMap', () => {
  test('Kartverket topo orders its tile path row before column', () => {
    expect(resolveBaseMap({ preset: 'kartverket-topo' })).toEqual({
      source: {
        type: 'raster',
        tiles: ['https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png'],
        tileSize: 256,
        maxzoom: 18,
        attribution: '<a href="https://www.kartverket.no/">© Kartverket</a>',
      },
    });
  });

  test.each([
    ['kartverket-grey', 'https://cache.kartverket.no/v1/wmts/1.0.0/topograatone/default/webmercator/{z}/{y}/{x}.png'],
    [
      'kartverket-toporaster',
      'https://cache.kartverket.no/v1/wmts/1.0.0/toporaster/default/webmercator/{z}/{y}/{x}.png',
    ],
  ] as const)('%s uses its own Kartverket layer', (preset, tile) => {
    expect(resolveBaseMap({ preset })).toMatchObject({
      source: { tiles: [tile], attribution: expect.stringContaining('© Kartverket') },
    });
  });

  test('OpenStreetMap uses the standard column-before-row path and credits its contributors', () => {
    expect(resolveBaseMap({ preset: 'osm' })).toEqual({
      source: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        maxzoom: 19,
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      },
    });
  });

  test('a panel saved without Base map options gets the default', () => {
    expect(resolveBaseMap(undefined)).toEqual(resolveBaseMap({ preset: 'kartverket-topo' }));
  });

  test('a preset id this version does not know is refused rather than crashing the panel', () => {
    expect(resolveBaseMap({ preset: 'kartverket-retired' } as unknown as BaseMapOptions)).toEqual({
      problem: 'Unknown Base map "kartverket-retired". Choose another under Base map in the panel options.',
    });
  });

  describe('custom', () => {
    const custom = (fields: object) => resolveBaseMap({ preset: 'custom', custom: { attribution: '© Me', ...fields } });

    test('a standard tile template becomes a raster source with its size, zoom and attribution', () => {
      expect(
        custom({
          url: 'https://tiles.example.com/{z}/{x}/{y}.png',
          tileSize: 512,
          maxzoom: 20,
          attribution: '© Example',
        })
      ).toEqual({
        source: {
          type: 'raster',
          tiles: ['https://tiles.example.com/{z}/{x}/{y}.png'],
          tileSize: 512,
          maxzoom: 20,
          attribution: '© Example',
        },
      });
    });

    test.each(['', '   ', undefined])('is refused without attribution (%p)', (attribution) => {
      expect(custom({ url: 'https://tiles.example.com/{z}/{x}/{y}.png', attribution })).toEqual({
        problem: 'A custom Base map needs an attribution. Enter the credit line its provider requires.',
      });
    });

    test('custom attribution is shown as text, never interpreted as markup', () => {
      expect(
        custom({
          url: 'https://tiles.example.com/{z}/{x}/{y}.png',
          attribution: '<img src=x onerror="alert(1)"> Tom & Jerry',
        })
      ).toMatchObject({ source: { attribution: '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; Tom &amp; Jerry' } });
    });

    test.each([
      [{ tileSize: 0 }, 'Tile size'],
      [{ tileSize: 100.5 }, 'Tile size'],
      [{ tileSize: Number.NaN }, 'Tile size'],
      [{ maxzoom: -1 }, 'Max zoom'],
      [{ maxzoom: 30 }, 'Max zoom'],
    ])('is refused when %p is out of range, since panel JSON bypasses the editor', (fields, field) => {
      expect(custom({ url: 'https://tiles.example.com/{z}/{x}/{y}.png', ...fields })).toEqual({
        problem: expect.stringContaining(field),
      });
    });

    test('the URL scheme is case-insensitive', () => {
      expect(custom({ url: 'HTTPS://tiles.example.com/{z}/{x}/{y}.png' })).toHaveProperty('source');
    });

    test('a WMS bounding-box template is accepted as is, for MapLibre to fill per tile', () => {
      const url =
        'https://wms.example.com/wms?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=ortho&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&FORMAT=image/png';
      expect(custom({ url })).toMatchObject({ source: { tiles: [url] } });
    });

    test.each([
      '',
      'https://tiles.example.com/tile.png',
      'https://tiles.example.com/{z}/{x}.png',
      'ftp://tiles.example.com/{z}/{x}/{y}.png',
    ])('is refused when %p is not an http(s) tile template', (url) => {
      expect(custom({ url })).toEqual({
        problem:
          'A custom Base map needs an http(s) URL containing either {z}, {x} and {y}, or {bbox-epsg-3857} for a WMS service.',
      });
    });
  });
});
