import React, { useMemo } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveBaseMap } from '../model/baseMap';
import { trailScene } from '../model/trail';
import type { MapPanelOptions } from '../types';
import { MapView } from './MapView';
import { PanelMessage } from './PanelMessage';

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height }) => {
  const resolved = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  const { trailColumns, dockOrigin } = options;
  const trail = useMemo(
    // Ages are measured from when the data was fetched, which is "now" for everything on screen.
    () => trailScene(data.series, { trailColumns, dockOrigin }, data.request?.startTime),
    [data.series, data.request?.startTime, trailColumns, dockOrigin]
  );

  if ('problem' in resolved) {
    return <PanelMessage width={width} height={height} text={resolved.problem} />;
  }
  if ('problem' in trail) {
    return <PanelMessage width={width} height={height} text={trail.problem} />;
  }
  return <MapView baseMap={resolved.source} trail={trail.scene} width={width} height={height} />;
};
