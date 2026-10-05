import { PanelPlugin, type PanelOptionsEditorBuilder } from '@grafana/data';
import { getTemplateSrv } from '@grafana/runtime';
import { CalibrationEditor } from './components/CalibrationEditor';
import { MapPanel } from './components/MapPanel';
import { BASE_MAP_PRESETS, CUSTOM_BASE_MAP, MAX_ZOOM, TILE_SIZE, type CustomSlot } from './model/baseMap';
import { LAWN_PATH, migrateLawn, type Lawn } from './model/lawn';
import { CUSTOM_OVERLAY, DEFAULT_OVERLAY_OPACITY, OVERLAY_PRESETS } from './model/overlay';
import { DEFAULT_JOB_VARIABLE } from './model/selection';
import { CUSTOM_TERRAIN, TERRAIN_ENCODINGS, TERRAIN_PRESETS, terrainEnabled } from './model/terrain';
import { DEFAULT_TRAIL_COLUMNS, type TrailColumns } from './model/trailFrame';
import { VIEWS } from './model/view';
import { DEFAULT_ZONE_PROGRESS_COLUMNS, type ZoneProgressColumns } from './model/zoneProgress';
import type { MapPanelOptions } from './types';

/** A picker's choices, one per Preset. */
const presetOptions = (presets: Record<string, { label: string; description?: string }>) =>
  Object.entries(presets).map(([value, { label, description }]) => ({ value, label, description }));

/**
 * The editors every custom slot shares: its URL, attribution, tile size and max zoom. What the slot
 * accepts and defaults to comes from the model, which checks the same slot.
 */
const addCustomSlot = (
  builder: PanelOptionsEditorBuilder<MapPanelOptions>,
  path: 'baseMap' | 'terrain' | 'overlay',
  slot: CustomSlot,
  showIf: (options: MapPanelOptions) => boolean
) => {
  const category = [slot.name];
  const template = slot.wms
    ? 'A tile URL with {z}, {x} and {y}, or a WMS GetMap URL with BBOX={bbox-epsg-3857}.'
    : 'A tile URL with {z}, {x} and {y}.';
  return builder
    .addTextInput({
      path: `${path}.custom.url`,
      name: 'URL template',
      description: `${template} The host must allow cross-origin requests.`,
      category,
      settings: { placeholder: 'https://tiles.example.com/{z}/{x}/{y}.png' },
      showIf,
    })
    .addTextInput({
      path: `${path}.custom.attribution`,
      name: 'Attribution',
      description: 'Required. The credit line the tile provider asks for, always shown on the map.',
      category,
      showIf,
    })
    .addNumberInput({
      path: `${path}.custom.tileSize`,
      name: 'Tile size',
      category,
      defaultValue: slot.tileSize,
      settings: { ...TILE_SIZE, integer: true },
      showIf,
    })
    .addNumberInput({
      path: `${path}.custom.maxzoom`,
      name: 'Max zoom',
      description: 'Highest zoom the service provides; the map enlarges tiles beyond it.',
      category,
      defaultValue: slot.maxzoom,
      settings: { ...MAX_ZOOM, integer: true },
      showIf,
    });
};

/** A text input per column of a query, in the order the options pane shows them. */
type ColumnEditors<T> = Array<{ key: keyof T & string; name: string; description: string }>;

// Blank means the default name, shown as the placeholder, so a database that names things the
// collector's way needs no settings at all.
const addColumnEditors = <T extends Record<keyof T, string>>(
  builder: PanelOptionsEditorBuilder<MapPanelOptions>,
  path: 'trailColumns' | 'zoneProgressColumns',
  category: string,
  editors: ColumnEditors<T>,
  defaults: T
) => {
  for (const { key, name, description } of editors) {
    builder.addTextInput({
      path: `${path}.${key}`,
      name,
      description,
      category: [category],
      settings: { placeholder: defaults[key] },
    });
  }
};

const TRAIL_COLUMN_EDITORS: ColumnEditors<TrailColumns> = [
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
      'Optional. Each Job is drawn as its own line, in its own colour, and named when hovering it. With SQL, use Format as: Table, which keeps text columns like this one as columns.',
  },
  {
    key: 'zone',
    name: 'Zone',
    description: 'Optional. The Zone each position was mowed in, shown when hovering the Trail.',
  },
  {
    key: 'status',
    name: 'Status',
    description:
      "Optional. The mower's state at each position, such as mowing or returning, shown when hovering the Trail.",
  },
  {
    key: 'mower',
    name: 'Mower',
    description:
      'Optional. Which mower reported each position. The panel warns when it is given more than one mower.',
  },
];

// The optional Zone progress query.
const ZONE_PROGRESS_COLUMN_EDITORS: ColumnEditors<ZoneProgressColumns> = [
  { key: 'zone', name: 'Zone', description: 'The identifier of the Zone, as given to it when it was traced.' },
  { key: 'progress', name: 'Progress', description: 'How far through the Zone the mower is, from 0 to 100.' },
  {
    key: 'time',
    name: 'Time',
    description: 'Optional. When the progress was reported; the latest row of each Zone is used.',
  },
];

export const plugin = new PanelPlugin<MapPanelOptions>(MapPanel)
  // Panels saved before the Lawn was kept by mower: under `lawn`, or as a Dock origin at the root.
  .setMigrationHandler((panel) => migrateLawn(panel.options))
  .setPanelOptions((builder) => {
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
  addCustomSlot(builder, 'baseMap', CUSTOM_BASE_MAP, (options) => options.baseMap?.preset === 'custom');

  const terrainOn = (options: MapPanelOptions) => terrainEnabled(options.terrain);
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
  addCustomSlot(builder, 'terrain', CUSTOM_TERRAIN, terrainCustom).addRadio({
    path: 'terrain.custom.encoding',
    name: 'Encoding',
    description: 'How the tiles hold elevation in their colours.',
    category: ['Terrain'],
    defaultValue: CUSTOM_TERRAIN.encoding,
    settings: { options: TERRAIN_ENCODINGS },
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
  addCustomSlot(builder, 'overlay', CUSTOM_OVERLAY, (options) => options.overlay?.preset === 'custom');

  const lawn = ['Dock origin and Boundary'];
  builder
    .addCustomEditor<unknown, Lawn | undefined>({
      id: 'lawn',
      path: LAWN_PATH,
      name: 'On the map',
      description:
        'Drag the dock into place, turn the Trail onto the lawn, and trace the lawn and its Zones. The fields below hold the same values.',
      category: lawn,
      editor: CalibrationEditor,
    })
    .addNumberInput({
      path: `${LAWN_PATH}.dockOrigin.lat`,
      name: 'Latitude',
      description: 'Of the charging dock, in decimal degrees.',
      category: lawn,
      settings: { min: -85, max: 85 },
    })
    .addNumberInput({
      path: `${LAWN_PATH}.dockOrigin.lon`,
      name: 'Longitude',
      description: 'Of the charging dock, in decimal degrees.',
      category: lawn,
      settings: { min: -180, max: 180 },
    })
    .addNumberInput({
      path: `${LAWN_PATH}.dockOrigin.rotation`,
      name: 'Rotation',
      description:
        "Compass bearing of the mower's x-axis, in degrees clockwise from north. Turn it until the Trail lies on the lawn.",
      category: lawn,
      defaultValue: 0,
    });
  addColumnEditors(builder, 'trailColumns', 'Trail columns', TRAIL_COLUMN_EDITORS, DEFAULT_TRAIL_COLUMNS);
  addColumnEditors(
    builder,
    'zoneProgressColumns',
    'Zone progress columns',
    ZONE_PROGRESS_COLUMN_EDITORS,
    DEFAULT_ZONE_PROGRESS_COLUMNS
  );
  builder
    .addSelect({
      path: 'jobVariable',
      name: 'Job variable',
      description:
        'The dashboard variable that holds a Job. Clicking a Trail sets it to that Job, and while it holds one the map draws that Job alone. Without the variable on the dashboard, every Job is drawn.',
      category: ['Jobs'],
      defaultValue: DEFAULT_JOB_VARIABLE,
      settings: {
        options: [],
        // The dashboard's own variables, and the default whether or not the dashboard has it yet.
        getOptions: async () =>
          [...new Set([DEFAULT_JOB_VARIABLE, ...getTemplateSrv().getVariables().map((v) => `$${v.name}`)])].map(
            (value) => ({ value, label: value })
          ),
      },
    })
    .addBooleanSwitch({
      path: 'follow',
      name: 'Follow the mower',
      description:
        'Starts the panel with the mower kept in the middle of the map, as for a wall display. The button on the panel turns it on and off from there.',
      category: ['Map view'],
      defaultValue: false,
    });
  return builder;
});
