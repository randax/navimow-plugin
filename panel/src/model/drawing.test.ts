import { closeDraft, EMPTY_DRAFT, placeVertex, type Draft } from './drawing';

const at = (lon: number, lat: number, px: number, py: number, time: number) => ({
  lngLat: [lon, lat] as [number, number],
  pixel: [px, py] as [number, number],
  time,
});

const after = (draft: Draft, event: ReturnType<typeof at>): Draft => {
  const step = placeVertex(draft, event);
  if (!('draft' in step)) {
    throw new Error(`expected a draft, got ${JSON.stringify(step)}`);
  }
  return step.draft;
};

/** Three clicks, one a second, well apart on screen. */
const triangle = (): Draft =>
  [at(10, 60, 0, 0, 0), at(10.001, 60, 100, 0, 1000), at(10.001, 60.001, 100, 100, 2000)].reduce(after, EMPTY_DRAFT);

describe('placeVertex', () => {
  test('each click adds a corner where it landed', () => {
    const step = placeVertex(EMPTY_DRAFT, at(10, 60, 50, 50, 0));
    expect(step).toEqual({ draft: { vertices: [[10, 60]], pixels: [[50, 50]], lastClick: 0 } });
  });

  test('the second click of a double click is the same click, not a second corner', () => {
    const draft = after(after(EMPTY_DRAFT, at(10, 60, 50, 50, 0)), at(10.0000001, 60, 52, 51, 180));
    expect(draft.vertices).toEqual([[10, 60]]);
  });

  test('a slow second click at the same place is a corner of its own', () => {
    const draft = after(after(EMPTY_DRAFT, at(10, 60, 50, 50, 0)), at(10, 60, 50, 50, 2000));
    expect(draft.vertices).toHaveLength(2);
  });

  test('a quick click somewhere else is a corner of its own', () => {
    const draft = after(after(EMPTY_DRAFT, at(10, 60, 50, 50, 0)), at(10.001, 60, 150, 50, 100));
    expect(draft.vertices).toHaveLength(2);
  });

  test('a click back on the first corner closes the ring without adding it again', () => {
    expect(placeVertex(triangle(), at(10.00001, 60, 3, 2, 5000))).toEqual({
      ring: [
        [10, 60],
        [10.001, 60],
        [10.001, 60.001],
      ],
    });
  });

  test('a click back on the first corner with only two corners is a corner, not a ring', () => {
    const two = after(after(EMPTY_DRAFT, at(10, 60, 0, 0, 0)), at(10.001, 60, 100, 0, 1000));
    expect(after(two, at(10, 60, 2, 2, 2000)).vertices).toHaveLength(3);
  });
});

describe('closeDraft', () => {
  test('a double click closes the ring, its own two clicks adding one corner between them', () => {
    const draft = after(after(triangle(), at(10, 60.001, 0, 100, 4000)), at(10, 60.001, 1, 100, 4150));
    expect(closeDraft(draft)).toEqual({
      ring: [
        [10, 60],
        [10.001, 60],
        [10.001, 60.001],
        [10, 60.001],
      ],
    });
  });

  test('fewer than three corners cannot close', () => {
    expect(closeDraft(after(EMPTY_DRAFT, at(10, 60, 0, 0, 0)))).toEqual({
      problem: expect.stringContaining('three'),
    });
  });
});
