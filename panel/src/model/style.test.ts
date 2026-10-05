import { resolveBaseMap } from './baseMap';
import { boundaryFeatures } from './lawn';
import { resolveOverlay } from './overlay';
import { mapStyle } from './style';
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
const boundary = boundaryFeatures({
  outline: [
    [10, 60],
    [10.001, 60],
    [10, 60.001],
  ],
});

describe('mapStyle', () => {
  test('a Base map alone is drawn under the Boundary and the Trail, with no Terrain', () => {
    const style = mapStyle({ baseMap }, lines, boundary);
    expect(style.layers.map((l) => l.id)).toEqual(['base', 'boundary-fill', 'boundary-line', 'trail']);
    expect(style.sources.base).toEqual(baseMap);
    expect(style.sources.trail).toEqual({ type: 'geojson', data: lines });
    expect(style.sources.boundary).toEqual({ type: 'geojson', data: boundary });
    expect(style.terrain).toBeUndefined();
  });

  test('the Overlay is drawn between the Base map and the Boundary, at its opacity', () => {
    const style = mapStyle({ baseMap, overlay }, lines, boundary);
    expect(style.layers.map((l) => l.id)).toEqual(['base', 'overlay', 'boundary-fill', 'boundary-line', 'trail']);
    expect(style.layers[1]).toMatchObject({
      id: 'overlay',
      type: 'raster',
      source: 'overlay',
      paint: { 'raster-opacity': 0.3 },
    });
    expect(style.sources.overlay).toEqual(overlay?.source);
  });

  test('tiles are drawn as they are, not faded in: a slow renderer can come to rest part-way through a fade', () => {
    const style = mapStyle({ baseMap, overlay }, lines);
    expect(style.layers.slice(0, 2).map((l) => l.paint)).toEqual([
      { 'raster-fade-duration': 0 },
      { 'raster-opacity': 0.3, 'raster-fade-duration': 0 },
    ]);
  });

  test('Terrain is declared in the style itself, so the map starts out on the ground', () => {
    const style = mapStyle({ baseMap, terrain }, lines);
    expect(style.sources.terrain).toEqual(terrain);
    expect(style.terrain).toEqual({ source: 'terrain' });
  });

  test('the Boundary and the Trail are fill and line layers, which the map lays over the Terrain', () => {
    const style = mapStyle({ baseMap, overlay, terrain }, lines, boundary);
    expect(style.layers.map((l) => [l.id, l.type])).toEqual([
      ['base', 'raster'],
      ['overlay', 'raster'],
      ['boundary-fill', 'fill'],
      ['boundary-line', 'line'],
      ['trail', 'line'],
    ]);
  });

  test('the Boundary is drawn even when it is empty, so a first Zone needs no restyle', () => {
    const style = mapStyle({ baseMap }, lines);
    expect(style.sources.boundary).toEqual({ type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  });

  test('a Zone is filled by its progress, from a pale wash to a deep one, and faintly where none is reported', () => {
    const fill = mapStyle({ baseMap }, lines, boundary).layers.find((l) => l.id === 'boundary-fill');
    expect(fill?.paint).toEqual({
      'fill-color': [
        'case',
        ['has', 'progress'],
        ['interpolate', ['linear'], ['get', 'progress'], 0, '#B2DFDB', 100, '#00695C'],
        ['match', ['get', 'kind'], 'outline', '#2E7D32', '#00897B'],
      ],
      'fill-opacity': ['case', ['has', 'progress'], 0.55, 0.12],
    });
  });

  const visibility = (hidden: Parameters<typeof mapStyle>[3]) =>
    mapStyle({ baseMap, overlay }, lines, boundary, hidden).layers.map((l) => [
      l.id,
      l.layout?.visibility ?? 'visible',
    ]);

  test('every layer is drawn until the owner hides one', () => {
    expect(visibility(undefined).map(([, shown]) => shown)).toEqual(Array(5).fill('visible'));
  });

  test('a hidden Trail or Boundary stays in the style, so showing it again needs no new data, but is not drawn', () => {
    expect(visibility(['boundary'])).toEqual([
      ['base', 'visible'],
      ['overlay', 'visible'],
      ['boundary-fill', 'none'],
      ['boundary-line', 'none'],
      ['trail', 'visible'],
    ]);
    expect(visibility(['trail', 'boundary']).slice(2)).toEqual([
      ['boundary-fill', 'none'],
      ['boundary-line', 'none'],
      ['trail', 'none'],
    ]);
  });

  test('the pickers are independent: any Base map combines with any Terrain and Overlay, each with its attribution', () => {
    const style = mapStyle(
      { baseMap, overlay, terrain: source(resolveTerrain({ enabled: true, preset: 'aws-terrarium' })) },
      lines
    );
    expect(Object.values(style.sources).map((s) => ('attribution' in s ? s.attribution : undefined))).toEqual([
      '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      '<a href="https://www.kartverket.no/">© Kartverket</a>',
      'Norway terrain data © Kartverket',
      undefined,
      undefined,
    ]);
  });
});
