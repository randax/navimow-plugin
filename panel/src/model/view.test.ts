import type { TerrainOptions } from './terrain';
import { cameraFor, initialView, viewState, type Camera, type View, type ViewState } from './view';

describe('initialView', () => {
  test('without Terrain the map is flat, whatever the saved start', () => {
    expect(initialView(undefined)).toBe('flat');
    expect(initialView({ enabled: false, startIn: 'terrain' })).toBe('flat');
  });

  test('with Terrain enabled the panel opens in terrain, so enabling it shows something', () => {
    expect(initialView({ enabled: true })).toBe('terrain');
  });

  test('the owner can have it open flat, with terrain a switch away', () => {
    expect(initialView({ enabled: true, startIn: 'flat' })).toBe('flat');
  });
});

describe('viewState', () => {
  /** The options as they change over time, and the owner's switches on the panel in between. */
  const after = (...steps: Array<TerrainOptions | View>) =>
    steps.reduce<ViewState | undefined>(
      (state, step) => (typeof step === 'string' ? state && { ...state, view: step } : viewState(step, state)),
      undefined
    )?.view;
  const startsIn = (startIn: View): TerrainOptions => ({ enabled: true, startIn });

  test('the panel opens in the view its options start in', () => {
    expect(after(startsIn('terrain'))).toBe('terrain');
    expect(after(startsIn('flat'))).toBe('flat');
    expect(after({ enabled: false })).toBe('flat');
  });

  test.each([null, { enabled: 'true' }, { enabled: 'false' }, { enabled: 1 }])(
    'options saved as %p leave Terrain off, as they do for the map, so the view is flat',
    (saved) => {
      expect(viewState(saved as unknown as TerrainOptions)).toEqual({ start: undefined, view: 'flat' });
      expect(initialView(saved as unknown as TerrainOptions)).toBe('flat');
    }
  );

  test("the owner's switch on the panel holds while the options stay as they are", () => {
    expect(after(startsIn('terrain'), 'flat', startsIn('terrain'))).toBe('flat');
    expect(after(startsIn('terrain'), 'flat', { enabled: true, startIn: 'terrain', preset: 'aws-terrarium' })).toBe(
      'flat'
    );
  });

  test('a new start in the options is followed, so editing them shows', () => {
    expect(after(startsIn('flat'), 'terrain', startsIn('terrain'), startsIn('flat'))).toBe('flat');
  });

  test('going back to an earlier start does not bring back the switch made under it', () => {
    expect(after(startsIn('terrain'), 'flat', startsIn('flat'), startsIn('terrain'))).toBe('terrain');
  });

  test('turning Terrain off and on again starts over, even where both start flat', () => {
    expect(after(startsIn('flat'), 'terrain', { enabled: false, startIn: 'flat' }, startsIn('flat'))).toBe('flat');
    expect(after(startsIn('terrain'), 'flat', { enabled: false }, startsIn('terrain'))).toBe('terrain');
  });
});

describe('cameraFor', () => {
  const camera: Camera = { center: [10.672, 59.964], zoom: 18.5, bearing: 25, pitch: 0 };

  test('switching to terrain keeps the place, zoom and bearing, and tilts the camera to show relief', () => {
    expect(cameraFor('terrain', { view: 'flat', camera })).toEqual({
      center: [10.672, 59.964],
      zoom: 18.5,
      bearing: 25,
      pitch: 60,
    });
  });

  test('switching to flat looks straight down at the same place', () => {
    expect(cameraFor('flat', { view: 'terrain', camera: { ...camera, pitch: 70 } })).toEqual(camera);
  });

  test('a flat map the owner had tilted keeps its tilt in terrain', () => {
    expect(cameraFor('terrain', { view: 'flat', camera: { ...camera, pitch: 35 } })).toEqual({ ...camera, pitch: 35 });
  });

  test('a new Terrain source in the terrain view keeps the whole camera, a straight-down one included', () => {
    expect(cameraFor('terrain', { view: 'terrain', camera })).toEqual(camera);
    expect(cameraFor('terrain', { view: 'terrain', camera: { ...camera, pitch: 35 } })).toEqual({
      ...camera,
      pitch: 35,
    });
  });

  test('a first map has no place to keep yet, only the tilt of its view', () => {
    expect(cameraFor('terrain')).toEqual({ pitch: 60 });
    expect(cameraFor('flat')).toEqual({ pitch: 0 });
  });
});
