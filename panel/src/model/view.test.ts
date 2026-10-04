import { cameraFor, initialView, type Camera } from './view';

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

describe('cameraFor', () => {
  const camera: Camera = { center: [10.672, 59.964], zoom: 18.5, bearing: 25, pitch: 0 };

  test('switching to terrain keeps the place, zoom and bearing, and tilts the camera to show relief', () => {
    expect(cameraFor('terrain', camera)).toEqual({ center: [10.672, 59.964], zoom: 18.5, bearing: 25, pitch: 60 });
  });

  test('switching to flat looks straight down at the same place', () => {
    expect(cameraFor('flat', { ...camera, pitch: 70 })).toEqual(camera);
  });

  test("a tilt the owner already chose is kept in terrain, such as when only the Terrain's source changes", () => {
    expect(cameraFor('terrain', { ...camera, pitch: 35 })).toEqual({ ...camera, pitch: 35 });
  });
});
