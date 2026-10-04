import React, { useEffect, useMemo, useState } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveBaseMap } from '../model/baseMap';
import { mowerAt, trailScene } from '../model/trail';
import type { MapPanelOptions } from '../types';
import { MapView } from './MapView';
import { PanelMessage } from './PanelMessage';

/** The wall clock, re-read every minute so a mower that stops reporting goes stale even with refresh off. */
const useNow = (): number => {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
};

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height }) => {
  const resolved = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  const { trailColumns, dockOrigin } = options;
  const trail = useMemo(
    () => trailScene(data.series, { trailColumns, dockOrigin }),
    [data.series, trailColumns, dockOrigin]
  );
  // Aged separately, so the minute tick restyles the marker without placing the Trail again.
  const now = useNow();
  const last = 'scene' in trail ? trail.scene.mower : undefined;
  const mower = useMemo(() => last && mowerAt(last, now), [last, now]);

  if ('problem' in resolved) {
    return <PanelMessage width={width} height={height} text={resolved.problem} />;
  }
  if ('problem' in trail) {
    return <PanelMessage width={width} height={height} text={trail.problem} />;
  }
  return <MapView baseMap={resolved.source} trail={trail.scene} mower={mower} width={width} height={height} />;
};
