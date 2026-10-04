import type { DockOrigin } from './dockOrigin';
import { nextFraming } from './framing';
import { EMPTY_SCENE, placeTrails } from './trail';
import type { TrailPoint } from './trailFrame';

const ORIGIN: DockOrigin = { lat: 59.964, lon: 10.672, rotation: 20 };

// The opening docked stretch of fixtures/trail-2026-09-21.csv: the mower in its dock, jittering.
const DOCKED: TrailPoint[] = [
  [1789986187389, -0.31, -0.357],
  [1789986188447, -0.31, -0.358],
  [1789986487389, -0.322, -0.39],
  [1789986787389, -0.286, -0.363],
  [1789987087389, -0.314, -0.41],
  [1789987387389, -0.305, -0.44],
  [1789987687389, -0.273, -0.371],
  [1789987952922, -0.24, -0.25],
  [1789987955274, -0.366, -0.457],
].map(([time, x, y]) => ({ time, x, y }));

const MOWING: TrailPoint[] = [...DOCKED, { time: 1789988000000, x: 12, y: 4 }, { time: 1789988002000, x: 20, y: -6 }];

const scene = (points: TrailPoint[], origin = ORIGIN) => placeTrails([{ segments: [points] }], origin);

describe('nextFraming', () => {
  test('a Trail is framed when it first appears', () => {
    expect(nextFraming(scene(MOWING), undefined)).toEqual({ origin: '59.964,10.672', wide: true });
  });

  test('nothing to draw frames nothing', () => {
    expect(nextFraming(EMPTY_SCENE, undefined)).toBeUndefined();
  });

  test("a docked mower's jitter is not a Trail with extent", () => {
    expect(nextFraming(scene(DOCKED), undefined)).toMatchObject({ wide: false });
  });

  test('a Trail first framed while docked is framed once more when the mower sets off, then left alone', () => {
    const docked = nextFraming(scene(DOCKED), undefined);
    const mowing = nextFraming(scene(MOWING), docked);
    expect(mowing).toMatchObject({ wide: true });
    expect(nextFraming(scene([...MOWING, { time: 1789988004000, x: 21, y: -7 }]), mowing)).toBeUndefined();
  });

  test('while still docked, each refresh frames the dock again', () => {
    const docked = nextFraming(scene(DOCKED.slice(0, 4)), undefined);
    expect(nextFraming(scene(DOCKED), docked)).toMatchObject({ wide: false });
  });

  test('moving the dock frames the Trail again', () => {
    const framed = nextFraming(scene(MOWING), undefined);
    expect(nextFraming(scene(MOWING, { ...ORIGIN, lat: 59.974 }), framed)).toEqual({
      origin: '59.974,10.672',
      wide: true,
    });
  });

  test('turning the Trail about the dock keeps the view', () => {
    const framed = nextFraming(scene(MOWING), undefined);
    expect(nextFraming(scene(MOWING, { ...ORIGIN, rotation: 21 }), framed)).toBeUndefined();
  });
});
