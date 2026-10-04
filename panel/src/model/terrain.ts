// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterDEMSourceSpecification } from 'maplibre-gl';
import { customTiles, KARTVERKET_ATTRIBUTION, type CustomSlot, type CustomSourceOptions } from './baseMap';

export type TerrainPreset = 'mapterhorn' | 'aws-terrarium';

/** How a tile's colours hold elevation. */
export const TERRAIN_ENCODINGS = ['terrarium', 'mapbox'] as const;
export type TerrainEncoding = (typeof TERRAIN_ENCODINGS)[number];

/** The two views the owner switches between on the panel: the map from above, or tilted over its relief. */
export type View = 'flat' | 'terrain';

export interface TerrainOptions {
  enabled?: boolean;
  preset?: TerrainPreset | 'custom';
  custom?: CustomSourceOptions & { encoding?: TerrainEncoding };
  /** The view the panel opens in; absent means terrain. */
  startIn?: View;
}

/** A source to draw relief from, nothing when Terrain is off, or a problem to show the owner instead of a map. */
export type ResolvedTerrain = { source?: RasterDEMSourceSpecification } | { problem: string };

interface Preset {
  label: string;
  description: string;
  source: RasterDEMSourceSpecification;
}

// Both are worldwide tile sets that carry Norway's national elevation model, at different detail.
export const TERRAIN_PRESETS: Record<TerrainPreset, Preset> = {
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
export const CUSTOM_TERRAIN: CustomSlot = { name: 'Terrain', wms: false, tileSize: 512, maxzoom: 16 };

// Off until enabled; once enabled, the higher-resolution Preset unless the owner picks another.
export function resolveTerrain({ enabled, preset = 'mapterhorn', custom }: TerrainOptions = {}): ResolvedTerrain {
  if (!enabled) {
    return {};
  }
  if (preset !== 'custom') {
    const known = Object.hasOwn(TERRAIN_PRESETS, preset) ? TERRAIN_PRESETS[preset] : undefined;
    return known
      ? { source: known.source }
      : { problem: `Unknown Terrain "${preset}". Choose another under Terrain in the panel options.` };
  }
  const tiles = customTiles(CUSTOM_TERRAIN, custom);
  if ('problem' in tiles) {
    return tiles;
  }
  const encoding = custom?.encoding ?? 'terrarium';
  return TERRAIN_ENCODINGS.includes(encoding)
    ? { source: { type: 'raster-dem', ...tiles, encoding } }
    : { problem: `A custom Terrain needs its encoding set to ${TERRAIN_ENCODINGS.join(' or ')}.` };
}
