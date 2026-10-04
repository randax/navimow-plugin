import { resolveBaseMap } from './baseMap';
import { resolveOverlay } from './overlay';
import { mapStyle, resolveSources } from './style';
import { resolveTerrain } from './terrain';
import { EMPTY_SCENE } from './trail';

const source = <T>(resolved: { source?: T } | { problem: string }): T => {
  if ('problem' in resolved || !resolved.source) {
    throw new Error('expected a source');
  }
  return resolved.source;
};
const baseMap = source(resolveBaseMap({ preset: 'osm' }));
const terrain = source(resolveTerrain({ enabled: true }));
const hillshade = resolveOverlay({ preset: 'kartverket-hillshade', opacity: 0.3 });
const overlay = 'overlay' in hillshade ? hillshade.overlay : undefined;
const { lines } = EMPTY_SCENE;

describe('resolveSources', () => {
  test('the three pickers are independent: any Base map combines with any Terrain and Overlay', () => {
    expect(
      resolveSources({
        baseMap: { preset: 'osm' },
        terrain: { enabled: true, preset: 'aws-terrarium' },
        overlay: { preset: 'kartverket-hillshade', opacity: 0.3 },
      })
    ).toEqual({
      sources: {
        baseMap,
        terrain: source(resolveTerrain({ enabled: true, preset: 'aws-terrarium' })),
        overlay,
      },
    });
  });

  test('a panel saved before Terrain and Overlay existed draws its Base map alone', () => {
    expect(resolveSources({ baseMap: { preset: 'osm' } })).toEqual({ sources: { baseMap } });
  });

  test.each([
    ['Base map', { baseMap: { preset: 'custom' as const } }],
    ['Terrain', { terrain: { enabled: true, preset: 'custom' as const } }],
    ['Overlay', { overlay: { preset: 'custom' as const } }],
  ])('a problem with the %s is the problem shown', (slot, options) => {
    expect(resolveSources(options)).toEqual({ problem: expect.stringContaining(`A custom ${slot} needs`) });
  });
});

describe('mapStyle', () => {
  test('a Base map alone is drawn under the Trail, with no Terrain', () => {
    const style = mapStyle({ baseMap }, lines);
    expect(style.layers.map((l) => l.id)).toEqual(['base', 'trail']);
    expect(style.sources.base).toEqual(baseMap);
    expect(style.sources.trail).toEqual({ type: 'geojson', data: lines });
    expect(style.terrain).toBeUndefined();
  });

  test('the Overlay is drawn between the Base map and the Trail, at its opacity', () => {
    const style = mapStyle({ baseMap, overlay }, lines);
    expect(style.layers.map((l) => l.id)).toEqual(['base', 'overlay', 'trail']);
    expect(style.layers[1]).toEqual({
      id: 'overlay',
      type: 'raster',
      source: 'overlay',
      paint: { 'raster-opacity': 0.3 },
    });
    expect(style.sources.overlay).toEqual(overlay?.source);
  });

  test('Terrain is declared in the style itself, so the map starts out on the ground', () => {
    const style = mapStyle({ baseMap, terrain }, lines);
    expect(style.sources.terrain).toEqual(terrain);
    expect(style.terrain).toEqual({ source: 'terrain' });
  });

  test('the Trail is a line layer, which the map lays over the Terrain', () => {
    const style = mapStyle({ baseMap, overlay, terrain }, lines);
    expect(style.layers.map((l) => [l.id, l.type])).toEqual([
      ['base', 'raster'],
      ['overlay', 'raster'],
      ['trail', 'line'],
    ]);
  });

  test('any Base map combines with any Terrain and Overlay, each carrying its own attribution', () => {
    const style = mapStyle({ baseMap: source(resolveBaseMap({ preset: 'kartverket-grey' })), overlay, terrain }, lines);
    expect(Object.values(style.sources).map((s) => ('attribution' in s ? s.attribution : undefined))).toEqual([
      '<a href="https://www.kartverket.no/">© Kartverket</a>',
      '<a href="https://www.kartverket.no/">© Kartverket</a>',
      '<a href="https://mapterhorn.com/attribution">© Mapterhorn</a>',
      undefined,
    ]);
  });
});
