import React, { useMemo } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveBaseMap } from '../model/baseMap';
import { trailScene } from '../model/trail';
import type { MapPanelOptions } from '../types';
import { MapView } from './MapView';
import { PanelMessage } from './PanelMessage';

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height }) => {
  const resolved = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  // Ages are measured from when the data was fetched, which is "now" for everything on screen.
  const now = data.request?.startTime ?? data.timeRange.to.valueOf();
  const trail = useMemo(
    () => trailScene(data.series, options.trailColumns, options.dockOrigin, now),
    [data.series, options.trailColumns, options.dockOrigin, now]
  );

  if ('problem' in resolved) {
    return <PanelMessage width={width} height={height} text={resolved.problem} />;
  }
  if ('problem' in trail) {
    return <PanelMessage width={width} height={height} text={trail.problem} />;
  }
  return <MapView baseMap={resolved.source} trail={trail.scene} width={width} height={height} />;
};
