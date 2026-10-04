import type { BaseMapOptions } from './model/baseMap';
import type { Lawn } from './model/lawn';
import type { OverlayOptions } from './model/overlay';
import type { TerrainOptions } from './model/terrain';
import type { TrailColumns } from './model/trailFrame';

export interface MapPanelOptions {
  baseMap: BaseMapOptions;
  terrain?: TerrainOptions;
  overlay?: OverlayOptions;
  /** The Dock origin and the Boundary: edited as plain fields, as text, or on the map in the drawer. */
  lawn?: Lawn;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
}
