import type { BaseMapOptions } from './model/baseMap';
import type { TrailOptions } from './model/trail';

export interface MapPanelOptions extends TrailOptions {
  baseMap: BaseMapOptions;
}
