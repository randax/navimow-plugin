// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterDEMSourceSpecification } from 'maplibre-gl';
import {
  customTiles,
  KARTVERKET_ATTRIBUTION,
  presetSource,
  type CustomSlot,
  type CustomSourceOptions,
  type Preset,
} from './baseMap';
import type { View } from './view';

export type TerrainPreset = 'mapterhorn' | 'aws-terrarium';

/** How a tile's colours hold elevation, as the editor offers the choice. */
export type TerrainEncoding = 'terrarium' | 'mapbox';
export const TERRAIN_ENCODINGS: Array<{ value: TerrainEncoding; label: string }> = [
  { value: 'terrarium', label: 'Terrarium' },
  { value: 'mapbox', label: 'Mapbox' },
];

export interface TerrainOptions {
  enabled?: boolean;
  preset?: TerrainPreset | 'custom';
  custom?: CustomSourceOptions & { encoding?: TerrainEncoding };
  /** The view the panel opens in; absent means terrain. */
  startIn?: View;
}

/** A source to draw relief from, nothing when Terrain is off, or a problem to show the owner instead of a map. */
export type ResolvedTerrain = { source?: RasterDEMSourceSpecification } | { problem: string };

// Both are worldwide tile sets that carry Norway's national elevation model, at different detail.
export const TERRAIN_PRESETS: Record<TerrainPreset, Preset<RasterDEMSourceSpecification>> = {
  mapterhorn: {
    label: 'Mapterhorn',
    description: 'Worldwide, with 1 m detail in Norway from Kartverket.',
    source: {
      type: 'raster-dem',
      tiles: ['https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'],
      encoding: 'terrarium',
      tileSize: 512,
      maxzoom: 16,
      // Kartverket's own credit too, since its data is what is drawn; it is not left to the Base map.
      attribution: `<a href="https://mapterhorn.com/attribution">© Mapterhorn</a>, ${KARTVERKET_ATTRIBUTION}`,
    },
  },
  'aws-terrarium': {
    label: 'AWS Terrain Tiles',
    description: 'Worldwide, with 10 m detail in Norway from Kartverket.',
    source: {
      type: 'raster-dem',
      tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
      encoding: 'terrarium',
      tileSize: 256,
      maxzoom: 15,
      attribution: 'Norway terrain data © Kartverket',
    },
  },
};

// Elevation comes as tiles only: a WMS service draws pictures of the ground, which hold no heights.
export const CUSTOM_TERRAIN: CustomSlot & { encoding: TerrainEncoding } = {
  name: 'Terrain',
  wms: false,
  tileSize: 512,
  maxzoom: 16,
  encoding: 'terrarium',
};

/** Whether Terrain is on. Only `true` turns it on: saved by hand as the text "false", it stays off. */
export const terrainEnabled = (options?: TerrainOptions | null): boolean => options?.enabled === true;

// Off until enabled; once enabled, the higher-resolution Preset unless the owner picks another.
export function resolveTerrain(options?: TerrainOptions | null): ResolvedTerrain {
  if (!terrainEnabled(options)) {
    return {};
  }
  const preset = options?.preset ?? 'mapterhorn';
  if (preset !== 'custom') {
    return presetSource('Terrain', TERRAIN_PRESETS, preset);
  }
  const tiles = customTiles(CUSTOM_TERRAIN, options?.custom);
  if ('problem' in tiles) {
    return tiles;
  }
  const encoding = options?.custom?.encoding ?? CUSTOM_TERRAIN.encoding;
  return TERRAIN_ENCODINGS.some(({ value }) => value === encoding)
    ? { source: { type: 'raster-dem', ...tiles, encoding } }
    : {
        problem: `A custom Terrain needs its encoding set to ${TERRAIN_ENCODINGS.map(({ value }) => value).join(' or ')}.`,
      };
}
