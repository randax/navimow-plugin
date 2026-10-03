import { PanelPlugin } from '@grafana/data';
import { MapPanel } from './components/MapPanel';
import { BASE_MAP_PRESETS, type BaseMapPreset } from './model/baseMap';
import type { MapPanelOptions } from './types';

const category = ['Base map'];
const isCustom = (options: MapPanelOptions) => options.baseMap?.preset === 'custom';

export const plugin = new PanelPlugin<MapPanelOptions>(MapPanel).setPanelOptions((builder) =>
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
      settings: { min: 64, integer: true },
      showIf: isCustom,
    })
    .addNumberInput({
      path: 'baseMap.custom.maxzoom',
      name: 'Max zoom',
      description: 'Highest zoom the service provides; the map enlarges tiles beyond it.',
      category,
      defaultValue: 18,
      settings: { min: 0, max: 24, integer: true },
      showIf: isCustom,
    })
);
