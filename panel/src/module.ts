import { PanelPlugin } from '@grafana/data';
import { MapPanel } from './components/MapPanel';
import { BASE_MAP_PRESETS, MAX_ZOOM, TILE_SIZE, type BaseMapPreset } from './model/baseMap';
import { DEFAULT_TRAIL_COLUMNS, type TrailColumns } from './model/trailFrame';
import type { MapPanelOptions } from './types';

const category = ['Base map'];
const isCustom = (options: MapPanelOptions) => options.baseMap?.preset === 'custom';

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
  { key: 'job', name: 'Job', description: 'Optional. Each Job is drawn as its own line, in its own colour.' },
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
  builder
    .addSelect({
      path: 'baseMap.preset',
      name: 'Base map',
      category,
      defaultValue: 'kartverket-topo',
      settings: {
        options: [
          ...(Object.entries(BASE_MAP_PRESETS) as Array<[BaseMapPreset, (typeof BASE_MAP_PRESETS)[BaseMapPreset]]>).map(
            ([value, { label, description }]) => ({ value, label, description })
          ),
          { value: 'custom', label: 'Custom', description: 'Your own tile or WMS service' },
        ],
      },
    })
    .addTextInput({
      path: 'baseMap.custom.url',
      name: 'URL template',
      description:
        'A tile URL with {z}, {x} and {y}, or a WMS GetMap URL with BBOX={bbox-epsg-3857}. The host must allow cross-origin requests.',
      category,
      settings: { placeholder: 'https://tiles.example.com/{z}/{x}/{y}.png' },
      showIf: isCustom,
    })
    .addTextInput({
      path: 'baseMap.custom.attribution',
      name: 'Attribution',
      description: 'Required. The credit line the tile provider asks for, always shown on the map.',
      category,
      showIf: isCustom,
    })
    .addNumberInput({
      path: 'baseMap.custom.tileSize',
      name: 'Tile size',
      category,
      defaultValue: 256,
      settings: { ...TILE_SIZE, integer: true },
      showIf: isCustom,
    })
    .addNumberInput({
      path: 'baseMap.custom.maxzoom',
      name: 'Max zoom',
      description: 'Highest zoom the service provides; the map enlarges tiles beyond it.',
      category,
      defaultValue: 18,
      settings: { ...MAX_ZOOM, integer: true },
      showIf: isCustom,
    })
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
