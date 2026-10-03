import type { BaseMapOptions } from './model/baseMap';
import type { DockOriginOptions } from './model/dockOrigin';
import type { TrailColumns } from './model/trailFrame';

export interface MapPanelOptions {
  baseMap: BaseMapOptions;
  dockOrigin?: DockOriginOptions;
  /** Blank or absent means the default column name. */
  trailColumns?: Partial<TrailColumns>;
}
