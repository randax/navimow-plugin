import React, { useMemo } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveBaseMap } from '../model/baseMap';
import type { MapPanelOptions } from '../types';
import { MapView } from './MapView';
import { PanelMessage } from './PanelMessage';

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, width, height }) => {
  const resolved = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);

  if ('problem' in resolved) {
    return <PanelMessage width={width} height={height} text={resolved.problem} />;
  }
  return <MapView baseMap={resolved.source} width={width} height={height} />;
};
