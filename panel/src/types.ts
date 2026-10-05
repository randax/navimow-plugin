import type { BaseMapOptions } from './model/baseMap';
import type { LawnOptions } from './model/lawn';
import type { OverlayOptions } from './model/overlay';
import type { TerrainOptions } from './model/terrain';
import type { TrailColumns } from './model/trailFrame';
import type { ZoneProgressColumns } from './model/zoneProgress';

/**
 * The Lawn is the Dock origin and the Boundary, saved by mower: edited as plain fields, as text, or
 * on the map in the drawer.
 */
export interface MapPanelOptions extends LawnOptions {
  baseMap: BaseMapOptions;
  terrain?: TerrainOptions;
  overlay?: OverlayOptions;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
  /** Blank or absent means the default column name. */
  zoneProgressColumns?: Partial<ZoneProgressColumns>;
  /** The dashboard variable that holds the selected Job. Blank or absent means the default name. */
  jobVariable?: string;
  /** Whether the view starts out following the mower. */
  follow?: boolean;
}
