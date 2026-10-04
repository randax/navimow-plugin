import type { TerrainOptions, View } from './terrain';

/** Where the map is looking from: all a recreated map needs to pick up where the last one was. */
export interface Camera {
  center: [number, number];
  zoom: number;
  bearing: number;
  pitch: number;
}

// Looking straight down, relief is all but invisible; this tilt shows a slope as a slope.
const TERRAIN_PITCH = 60;

/** The view the panel opens in: terrain once Terrain is enabled, unless the owner has it start flat. */
export const initialView = ({ enabled, startIn }: TerrainOptions = {}): View =>
  enabled && startIn !== 'flat' ? 'terrain' : 'flat';

/**
 * Where a map recreated for `view` starts. It looks at the same place from the same distance and
 * bearing, so switching never throws the owner somewhere else; only the tilt follows the view.
 */
export const cameraFor = (view: View, previous: Camera): Camera => ({
  ...previous,
  pitch: view === 'flat' ? 0 : previous.pitch || TERRAIN_PITCH,
});
