import { toLonLat, type DockOrigin } from './dockOrigin';
import { jobAt, trailDetail, zoneDetail } from './hover';
import type { Zone } from './lawn';
import { placeTrails } from './trail';
import type { Trail, TrailPoint } from './trailFrame';

const ORIGIN: DockOrigin = { lat: 59.964, lon: 10.672, rotation: 20 };
// A real epoch in milliseconds, from the 2026-09-21 fixture.
const T = 1789986187389;
const SEC = 1000;
const ring: Zone['ring'] = [
  [10.672, 59.964],
  [10.673, 59.964],
  [10.673, 59.965],
];
const formatTime = (time: number) => new Date(time).toISOString();

const point = (seconds: number, x: number, y: number, extra: Partial<TrailPoint> = {}): TrailPoint => ({
  time: T + seconds * SEC,
  x,
  y,
  ...extra,
});
// Job a heads north along x in Zone 8 and on into Zone 9, with a gap between; Job b lies beside it.
const trails: Trail[] = [
  {
    job: 'a',
    segments: [
      [point(0, 0, 0, { zone: '8', status: 'isRunning' }), point(2, 1, 0, { zone: '8', status: 'isRunning' })],
      [point(900, 5, 0, { zone: '9' }), point(902, 6, 0, { zone: '9' })],
    ],
  },
  { job: 'b', segments: [[point(2000, 0, 3), point(2002, 1, 3)]] },
  { outsideJob: true, segments: [[point(3000, 0, 6), point(3002, 1, 6)]] },
];
const scene = placeTrails(trails, ORIGIN);
const near = (x: number, y: number) => toLonLat(ORIGIN, x, y);

describe('trailDetail', () => {
  test('tells the time, Job, Zone and status of the position nearest the pointer', () => {
    expect(trailDetail(scene, 0, near(0.9, 0.2), { formatTime })).toEqual({
      title: '2026-09-21T10:23:09.389Z',
      // The second colour of the palette, which is where the identifier "a" lands.
      colour: '#1F60C4',
      rows: [
        { label: 'Job', value: 'a' },
        { label: 'Zone', value: '8' },
        { label: 'Status', value: 'isRunning' },
      ],
    });
  });

  test('looks only along the Trail under the pointer, however close another runs', () => {
    expect(trailDetail(scene, 1, near(0.9, 0.2), { formatTime })).toMatchObject({
      title: '2026-09-21T10:56:29.389Z',
      rows: [{ label: 'Job', value: 'b' }],
    });
  });

  test('reaches across a gap to a later stretch of the same Trail', () => {
    expect(trailDetail(scene, 0, near(5.8, 0), { formatTime })?.title).toBe('2026-09-21T10:38:09.389Z');
  });

  test('names the Zone as the owner traced it, when the Boundary has it', () => {
    const zones = [{ id: '8', name: 'Front lawn', ring }];
    expect(trailDetail(scene, 0, near(0, 0), { formatTime, zones })?.rows).toContainEqual({
      label: 'Zone',
      value: 'Front lawn (8)',
    });
    expect(trailDetail(scene, 0, near(6, 0), { formatTime, zones })?.rows).toContainEqual({
      label: 'Zone',
      value: '9',
    });
  });

  test('says so for positions outside any Job, and leaves out whatever the data does not have', () => {
    expect(trailDetail(scene, 2, near(0, 6), { formatTime })?.rows).toEqual([
      { label: 'Job', value: 'Outside any Job' },
    ]);
    const bare = placeTrails([{ segments: [[point(0, 0, 0), point(2, 1, 0)]] }], ORIGIN);
    expect(trailDetail(bare, 0, near(0, 0), { formatTime })).toEqual({
      title: '2026-09-21T10:23:07.389Z',
      // Without a Job to name it, a Trail takes the palette in order: the first colour.
      colour: '#E02F44',
      rows: [],
    });
  });

  test('is nothing for a Trail the scene does not have', () => {
    expect(trailDetail(scene, 7, near(0, 0), { formatTime })).toBeUndefined();
    expect(trailDetail({ ...scene, trails: [{ segments: [] }] }, 0, near(0, 0), { formatTime })).toBeUndefined();
  });
});

describe('jobAt', () => {
  test('is the Job of the Trail under the pointer', () => {
    expect(jobAt(scene, 1)).toBe('b');
  });

  test('is nothing for positions outside any Job, which a click cannot select', () => {
    expect(jobAt(scene, 2)).toBeUndefined();
    expect(jobAt(scene, 7)).toBeUndefined();
  });
});

describe('zoneDetail', () => {
  const zones = [
    { id: '8', name: 'Front lawn', ring },
    { id: '9', name: '', ring },
  ];

  test('names the Zone and tells its latest progress, and when that was reported', () => {
    expect(zoneDetail(0, { formatTime, zones, progress: { '8': { progress: 63.6, time: T } } })).toEqual({
      title: 'Front lawn (8)',
      rows: [
        { label: 'Progress', value: '64%' },
        { label: 'Reported', value: '2026-09-21T10:23:07.389Z' },
      ],
    });
  });

  test('progress reported without a time is told without one', () => {
    expect(zoneDetail(1, { formatTime, zones, progress: { '9': { progress: 100 } } })).toEqual({
      title: '9',
      rows: [{ label: 'Progress', value: '100%' }],
    });
  });

  test('a Zone with no progress reported is only named', () => {
    expect(zoneDetail(0, { formatTime, zones })).toEqual({ title: 'Front lawn (8)', rows: [] });
  });

  test('is nothing for a Zone the Boundary does not have', () => {
    expect(zoneDetail(2, { formatTime, zones })).toBeUndefined();
  });

  test('two Zones traced under one identifier are each named as themselves, and share its progress', () => {
    const twins = ['Front', 'Back'].map((name) => ({ id: '1', name, ring }));
    const told = [0, 1].map((place) =>
      zoneDetail(place, { formatTime, zones: twins, progress: { '1': { progress: 40 } } })
    );
    expect(told.map((detail) => detail?.title)).toEqual(['Front (1)', 'Back (1)']);
    expect(told.map((detail) => detail?.rows)).toEqual(Array(2).fill([{ label: 'Progress', value: '40%' }]));
  });
});
