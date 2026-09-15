import { PanelPlugin } from '@grafana/data';
import { TerrainOptions } from './types';
import { TerrainPanel } from './components/TerrainPanel';

export const plugin = new PanelPlugin<TerrainOptions>(TerrainPanel).setPanelOptions((builder) =>
  builder
    .addBooleanSwitch({ path: 'terrain', name: '3D terrain', defaultValue: true })
    .addNumberInput({ path: 'exaggeration', name: 'Terrain exaggeration', defaultValue: 1.5 })
    .addNumberInput({ path: 'pitch', name: 'Camera pitch', defaultValue: 60 })
);
