import { terrainEnabled, type TerrainOptions } from './terrain';

/** The two views the owner switches between on the panel: the map from above, or tilted over its relief. */
export type View = 'flat' | 'terrain';

/** Both views, as the switch and the editor name them. */
export const VIEWS: Array<{ value: View; label: string }> = [
  { value: 'flat', label: 'Flat' },
  { value: 'terrain', label: 'Terrain' },
];

/** Where the map is looking from: all a recreated map needs to pick up where the last one was. */
export interface Camera {
  center: [number, number];
  zoom: number;
  bearing: number;
  pitch: number;
}

// Looking straight down, relief is all but invisible; this tilt shows a slope as a slope.
const TERRAIN_PITCH = 60;

/**
 * The tilt to give a map whose Coverage has just been raised, if it needs one. From straight above,
 * columns show only their tops, which is the flat picture again; a map already tilted is the owner's.
 */
export const pitchToSeeRaised = (pitch: number): number | undefined => (pitch < 1 ? TERRAIN_PITCH : undefined);

/** The view the panel opens in: terrain once Terrain is enabled, unless the owner has it start flat. */
export const initialView = (options?: TerrainOptions | null): View =>
  terrainEnabled(options) && options?.startIn !== 'flat' ? 'terrain' : 'flat';

/** The view the panel is in, and the start it was taken from: nothing while Terrain is off. */
export interface ViewState {
  start?: View;
  view: View;
}

/**
 * The view to show, given what was shown before. The owner's switch on the panel holds for as long
 * as the options start the panel the same way. When they change, by a new start or by Terrain going
 * off or on, the panel starts over from them, and an earlier switch is gone for good.
 */
export function viewState(options: TerrainOptions | null | undefined, previous?: ViewState): ViewState {
  const start = terrainEnabled(options) ? initialView(options) : undefined;
  return previous && previous.start === start ? previous : { start, view: start ?? 'flat' };
}

/** Whether the view follows the mower, and the option it was started from. */
export interface FollowState {
  start: boolean;
  following: boolean;
}

/**
 * Whether the view follows the mower, given what it did before. Off unless the options start it
 * on, so that a refresh never fights the owner's panning. Like the view, the owner's switch on the
 * panel holds until the option changes, and the panel then starts over from it.
 */
export function followState(option: boolean | undefined, previous?: FollowState): FollowState {
  const start = option === true;
  return previous && previous.start === start ? previous : { start, following: start };
}

/**
 * Where a map made for `view` starts. One that replaces another looks at the same place from the
 * same distance and bearing, so switching never throws the owner somewhere else. Only the tilt
 * follows a change of view: terrain is entered tilted, unless the owner had tilted the flat map
 * already, and flat is seen from straight above. A map replaced within its view, for a new Terrain
 * source, keeps the whole camera.
 */
export function cameraFor(view: View, previous?: { view: View; camera: Camera }): Partial<Camera> {
  if (previous?.view === view) {
    return previous.camera;
  }
  return { ...previous?.camera, pitch: view === 'flat' ? 0 : previous?.camera.pitch || TERRAIN_PITCH };
}
