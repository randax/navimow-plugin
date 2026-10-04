import { resolveOverlay, type OverlayOptions } from './overlay';

describe('resolveOverlay', () => {
  test('there is no Overlay unless one is chosen', () => {
    expect(resolveOverlay(undefined)).toEqual({});
    expect(resolveOverlay({ preset: 'none', opacity: 1 })).toEqual({});
  });

  test('options written by hand without a preset mean no Overlay too', () => {
    expect(resolveOverlay({ opacity: 0.4 })).toEqual({});
    expect(resolveOverlay({ custom: { url: 'https://t.example.com/{z}/{x}/{y}.png', attribution: '© Me' } })).toEqual(
      {}
    );
  });

  test("the Preset is Kartverket's hillshade, a WMS service asked for one bounding box per tile", () => {
    expect(resolveOverlay({ preset: 'kartverket-hillshade' })).toEqual({
      overlay: {
        source: {
          type: 'raster',
          tiles: [
            'https://wms.geonorge.no/skwms1/wms.hoyde-dtm?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=DTM:skyggerelieff&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&FORMAT=image/png',
          ],
          tileSize: 256,
          maxzoom: 18,
          attribution: '<a href="https://www.kartverket.no/">© Kartverket</a>',
        },
        opacity: 0.5,
      },
    });
  });

  test('a preset id this version does not know is refused rather than crashing the panel', () => {
    expect(resolveOverlay({ preset: 'retired' } as unknown as OverlayOptions)).toEqual({
      problem: 'Unknown Overlay "retired". Choose another under Overlay in the panel options.',
    });
  });

  describe('opacity', () => {
    const opacityOf = (opacity: number | undefined) => resolveOverlay({ preset: 'kartverket-hillshade', opacity });

    test("is the owner's", () => {
      expect(opacityOf(0.8)).toMatchObject({ overlay: { opacity: 0.8 } });
      expect(opacityOf(0)).toMatchObject({ overlay: { opacity: 0 } });
    });

    test.each([
      [1.5, 1],
      [-1, 0],
      [Number.NaN, 0.5],
    ])('%p from panel JSON, which bypasses the slider, is drawn as %p', (saved, drawn) => {
      expect(opacityOf(saved)).toMatchObject({ overlay: { opacity: drawn } });
    });
  });

  describe('custom', () => {
    const url = 'https://imagery.example.com/wms?LAYERS=ortho&BBOX={bbox-epsg-3857}&token=abc';

    test("the owner's own URL, such as imagery they hold a licence for, becomes the Overlay", () => {
      expect(
        resolveOverlay({
          preset: 'custom',
          custom: { url, attribution: '© Example', tileSize: 512, maxzoom: 20 },
          opacity: 1,
        })
      ).toEqual({
        overlay: {
          source: { type: 'raster', tiles: [url], tileSize: 512, maxzoom: 20, attribution: '© Example' },
          opacity: 1,
        },
      });
    });

    test("a tile template works as well, with the Base map slot's defaults", () => {
      expect(
        resolveOverlay({
          preset: 'custom',
          custom: { url: 'https://t.example.com/{z}/{x}/{y}.jpg', attribution: '© Me' },
        })
      ).toMatchObject({ overlay: { source: { tileSize: 256, maxzoom: 18 } } });
    });

    test('is refused without attribution', () => {
      expect(resolveOverlay({ preset: 'custom', custom: { url } })).toEqual({
        problem: 'A custom Overlay needs an attribution. Enter the credit line its provider requires.',
      });
    });

    test('is refused when the URL is not a tile or WMS template', () => {
      expect(
        resolveOverlay({ preset: 'custom', custom: { url: 'https://example.com/', attribution: '© Me' } })
      ).toEqual({
        problem: expect.stringContaining('A custom Overlay needs an http(s) URL'),
      });
    });
  });
});
