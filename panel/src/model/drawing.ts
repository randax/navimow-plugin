/**
 * Drawing a polygon on the map, one click per corner, closed by a double click or by clicking the
 * first corner again. Pure: the map hands in each click and takes back what to show.
 */

/** A polygon's corners in order, as [longitude, latitude]; open, the last joins back to the first. */
export type Ring = Array<[number, number]>;

/** A polygon in the making. */
export interface Draft {
  vertices: Ring;
  /** Where on screen each corner was clicked, so clicks are compared where the hand put them. */
  pixels: Array<[number, number]>;
  /** When the last corner was clicked, in milliseconds. */
  lastClick?: number;
}

export const EMPTY_DRAFT: Draft = { vertices: [], pixels: [] };

/** A click on the map: where, on the ground and on screen, and when. */
export interface Click {
  lngLat: [number, number];
  pixel: [number, number];
  time: number;
}

/** What a click did: changed the draft, finished a ring, or could not be followed. */
export type DrawStep = { draft: Draft } | { ring: Ring } | { problem: string };

// A double click reaches the map as two clicks and then a double click, so the second click would
// add the closing corner twice. Two clicks this close in time and on screen are one click, with
// room for the hand to move a little between them.
const SAME_CLICK_MS = 400;
const SAME_CLICK_PX = 6;
// A click this near the first corner means "close here": further than a double click's wobble, so
// that a corner placed next to the first stays a corner.
const CLOSE_PX = 10;

const FEWER_THAN_THREE = 'A Boundary needs at least three corners before it can close.';

const apart = ([ax, ay]: [number, number], [bx, by]: [number, number]) => Math.hypot(ax - bx, ay - by);

/** Adds a corner at the click, unless the click repeats the last one or returns to the first. */
export function placeVertex(draft: Draft, { lngLat, pixel, time }: Click): DrawStep {
  const last = draft.pixels.at(-1);
  if (
    last &&
    draft.lastClick !== undefined &&
    time - draft.lastClick <= SAME_CLICK_MS &&
    apart(last, pixel) <= SAME_CLICK_PX
  ) {
    return { draft };
  }
  if (draft.vertices.length >= 3 && apart(draft.pixels[0], pixel) <= CLOSE_PX) {
    return { ring: draft.vertices };
  }
  return {
    draft: { vertices: [...draft.vertices, lngLat], pixels: [...draft.pixels, pixel], lastClick: time },
  };
}

/** Closes the draft as it stands, on a double click. */
export function closeDraft(draft: Draft): DrawStep {
  return draft.vertices.length >= 3 ? { ring: draft.vertices } : { problem: FEWER_THAN_THREE };
}
