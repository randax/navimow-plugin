import type { BaseMapOptions } from './model/baseMap';
import type { OverlayOptions } from './model/overlay';
import type { TerrainOptions } from './model/terrain';
import type { TrailOptions } from './model/trail';

export interface MapPanelOptions extends TrailOptions {
  baseMap: BaseMapOptions;
  terrain?: TerrainOptions;
  overlay?: OverlayOptions;
}
