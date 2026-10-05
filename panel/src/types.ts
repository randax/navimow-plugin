import type { BaseMapOptions } from './model/baseMap';
import type { LawnOptions } from './model/lawn';
import type { OverlayOptions } from './model/overlay';
import type { TerrainOptions } from './model/terrain';
import type { TrailColumns } from './model/trailFrame';

/** `lawn` is the Dock origin and the Boundary: edited as plain fields, as text, or on the map in the drawer. */
export interface MapPanelOptions extends LawnOptions {
  baseMap: BaseMapOptions;
  terrain?: TerrainOptions;
  overlay?: OverlayOptions;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
}
