import { PanelPlugin, type PanelOptionsEditorBuilder } from '@grafana/data';
import { MapPanel } from './components/MapPanel';
import { VIEWS } from './components/ViewSwitch';
import { BASE_MAP_PRESETS, MAX_ZOOM, TILE_SIZE } from './model/baseMap';
import { DEFAULT_OVERLAY_OPACITY, OVERLAY_PRESETS } from './model/overlay';
import { TERRAIN_PRESETS } from './model/terrain';
import { DEFAULT_TRAIL_COLUMNS, type TrailColumns } from './model/trailFrame';
import type { MapPanelOptions } from './types';

/** A picker's choices, one per Preset. */
const presetOptions = (presets: Record<string, { label: string; description?: string }>) =>
  Object.entries(presets).map(([value, { label, description }]) => ({ value, label, description }));

const TILE_OR_WMS_URL = 'A tile URL with {z}, {x} and {y}, or a WMS GetMap URL with BBOX={bbox-epsg-3857}.';
const CROSS_ORIGIN = 'The host must allow cross-origin requests.';

/** The editors every custom slot shares: its URL, attribution, tile size and max zoom. */
const addCustomSlot = (
  builder: PanelOptionsEditorBuilder<MapPanelOptions>,
  slot: 'baseMap' | 'terrain' | 'overlay',
  {
    category,
    showIf,
    urlDescription,
    tileSize,
    maxzoom,
  }: {
    category: string[];
    showIf: (options: MapPanelOptions) => boolean;
    urlDescription: string;
    tileSize: number;
    maxzoom: number;
  }
) =>
  builder
    .addTextInput({
      path: `${slot}.custom.url`,
      name: 'URL template',
      description: urlDescription,
      category,
      settings: { placeholder: 'https://tiles.example.com/{z}/{x}/{y}.png' },
      showIf,
    })
    .addTextInput({
      path: `${slot}.custom.attribution`,
      name: 'Attribution',
      description: 'Required. The credit line the tile provider asks for, always shown on the map.',
      category,
      showIf,
    })
    .addNumberInput({
      path: `${slot}.custom.tileSize`,
      name: 'Tile size',
      category,
      defaultValue: tileSize,
      settings: { ...TILE_SIZE, integer: true },
      showIf,
    })
    .addNumberInput({
      path: `${slot}.custom.maxzoom`,
      name: 'Max zoom',
      description: 'Highest zoom the service provides; the map enlarges tiles beyond it.',
      category,
      defaultValue: maxzoom,
      settings: { ...MAX_ZOOM, integer: true },
      showIf,
    });

// Option editors for each Trail column, in the order the options pane shows them.
const TRAIL_COLUMN_EDITORS: Array<{ key: keyof TrailColumns; name: string; description: string }> = [
  { key: 'time', name: 'Time', description: 'When each position was recorded.' },
  { key: 'x', name: 'X', description: "Metres from the dock along the mower's x-axis." },
  { key: 'y', name: 'Y', description: "Metres from the dock along the mower's y-axis." },
  {
    key: 'heading',
    name: 'Heading',
    description: 'Optional. Radians counter-clockwise from the x-axis; points the mower marker.',
  },
  {
    key: 'job',
    name: 'Job',
    description:
      'Optional. Each Job is drawn as its own line, in its own colour. With SQL, use Format as: Table, which keeps text columns like this one as columns.',
  },
  {
    key: 'zone',
    name: 'Zone',
    description:
      'Optional. The Zone each position was mowed in. Read now, shown when hovering the Trail in an upcoming release.',
  },
  {
    key: 'status',
    name: 'Status',
    description:
      "Optional. The mower's state at each position, such as mowing or returning. Read now, shown when hovering the Trail in an upcoming release.",
  },
  {
    key: 'mower',
    name: 'Mower',
    description:
      'Optional. Which mower reported each position. Read now, used to warn about data from more than one mower in an upcoming release.',
  },
];

export const plugin = new PanelPlugin<MapPanelOptions>(MapPanel).setPanelOptions((builder) => {
  builder.addSelect({
    path: 'baseMap.preset',
    name: 'Base map',
    category: ['Base map'],
    defaultValue: 'kartverket-topo',
    settings: {
      options: [
        ...presetOptions(BASE_MAP_PRESETS),
        { value: 'custom', label: 'Custom', description: 'Your own tile or WMS service' },
      ],
    },
  });
  addCustomSlot(builder, 'baseMap', {
    category: ['Base map'],
    showIf: (options) => options.baseMap?.preset === 'custom',
    urlDescription: `${TILE_OR_WMS_URL} ${CROSS_ORIGIN}`,
    tileSize: 256,
    maxzoom: 18,
  });

  const terrainOn = (options: MapPanelOptions) => options.terrain?.enabled === true;
  const terrainCustom = (options: MapPanelOptions) => terrainOn(options) && options.terrain?.preset === 'custom';
  builder
    .addBooleanSwitch({
      path: 'terrain.enabled',
      // Not "Terrain": Grafana hides the name and description of an option named after its category.
      name: 'Enable',
      description: 'Draws the map over real relief, and adds a switch between flat and terrain to the panel.',
      category: ['Terrain'],
      defaultValue: false,
    })
    .addSelect({
      path: 'terrain.preset',
      name: 'Source',
      description: 'Where the elevation tiles come from.',
      category: ['Terrain'],
      defaultValue: 'mapterhorn',
      settings: {
        options: [
          ...presetOptions(TERRAIN_PRESETS),
          { value: 'custom', label: 'Custom', description: 'Your own elevation tiles' },
        ],
      },
      showIf: terrainOn,
    })
    .addRadio({
      path: 'terrain.startIn',
      name: 'Start in',
      description: 'The view the panel opens in.',
      category: ['Terrain'],
      defaultValue: 'terrain',
      settings: { options: VIEWS },
      showIf: terrainOn,
    });
  addCustomSlot(builder, 'terrain', {
    category: ['Terrain'],
    showIf: terrainCustom,
    urlDescription: `A tile URL with {z}, {x} and {y}. ${CROSS_ORIGIN}`,
    tileSize: 512,
    maxzoom: 16,
  });
  builder.addRadio({
    path: 'terrain.custom.encoding',
    name: 'Encoding',
    description: 'How the tiles hold elevation in their colours.',
    category: ['Terrain'],
    defaultValue: 'terrarium',
    settings: {
      options: [
        { value: 'terrarium', label: 'Terrarium' },
        { value: 'mapbox', label: 'Mapbox' },
      ],
    },
    showIf: terrainCustom,
  });

  const overlayOn = (options: MapPanelOptions) => (options.overlay?.preset ?? 'none') !== 'none';
  builder
    .addSelect({
      path: 'overlay.preset',
      name: 'Overlay',
      category: ['Overlay'],
      defaultValue: 'none',
      settings: {
        options: [
          { value: 'none', label: 'None' },
          ...presetOptions(OVERLAY_PRESETS),
          {
            value: 'custom',
            label: 'Custom',
            description: 'Your own tile or WMS service, such as imagery you hold a licence for',
          },
        ],
      },
    })
    .addSliderInput({
      path: 'overlay.opacity',
      name: 'Opacity',
      category: ['Overlay'],
      defaultValue: DEFAULT_OVERLAY_OPACITY,
      settings: { min: 0, max: 1, step: 0.05 },
      showIf: overlayOn,
    });
  addCustomSlot(builder, 'overlay', {
    category: ['Overlay'],
    showIf: (options) => options.overlay?.preset === 'custom',
    urlDescription: `${TILE_OR_WMS_URL} ${CROSS_ORIGIN}`,
    tileSize: 256,
    maxzoom: 18,
  });

  builder
    .addNumberInput({
      path: 'dockOrigin.lat',
      name: 'Latitude',
      description: 'Of the charging dock, in decimal degrees.',
      category: ['Dock origin'],
      settings: { min: -85, max: 85 },
    })
    .addNumberInput({
      path: 'dockOrigin.lon',
      name: 'Longitude',
      description: 'Of the charging dock, in decimal degrees.',
      category: ['Dock origin'],
      settings: { min: -180, max: 180 },
    })
    .addNumberInput({
      path: 'dockOrigin.rotation',
      name: 'Rotation',
      description:
        "Compass bearing of the mower's x-axis, in degrees clockwise from north. Turn it until the Trail lies on the lawn.",
      category: ['Dock origin'],
      defaultValue: 0,
    });
  // Blank means the default name, shown as the placeholder, so a database that names things the
  // collector's way needs no settings at all.
  for (const { key, name, description } of TRAIL_COLUMN_EDITORS) {
    builder.addTextInput({
      path: `trailColumns.${key}`,
      name,
      description,
      category: ['Trail columns'],
      settings: { placeholder: DEFAULT_TRAIL_COLUMNS[key] },
    });
  }
  return builder;
});
